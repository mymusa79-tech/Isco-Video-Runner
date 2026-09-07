from __future__ import annotations

"""Read-only certification gate for a historical QC_PENDING Gold resume source.

Canonical production still requires *current* protected main. Gold-only resume is a
narrower operation over already-rendered exact bytes, so it may use the historical
Runner SHA that created those bytes only when GitHub proves that SHA was itself a
protected-main production source with the same exact-SHA certification receipts.
"""

import argparse
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable


_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_RUN_RE = re.compile(r"^[1-9][0-9]*$")
_REQUIRED_TAG_PREFIXES = ("full-regression-green-", "stage-ladder-green-")
_REQUIRED_WORKFLOWS = (
    ("Verify Private Engine", ".github/workflows/verify-private-engine.yml"),
    ("Verify Production Stage Ladder", ".github/workflows/verify-production-stage-ladder.yml"),
)
_PRODUCTION_WORKFLOW = ".github/workflows/produce-resilient-v4.yml"


def _github_json(url: str, token: str, *, opener: Callable[..., Any] = urllib.request.urlopen) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "isco-qc-pending-resume-source-gate",
        },
    )
    try:
        with opener(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"QC_PENDING resume certification lookup failed for {url}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("QC_PENDING resume certification lookup returned non-object JSON")
    return payload


def _repo_path(repository: str) -> str:
    if not repository or repository.count("/") != 1:
        raise RuntimeError("repository must be owner/repository")
    return "/".join(urllib.parse.quote(part, safe="") for part in repository.split("/"))


def verify_historical_resume_source(
    *,
    repository: str,
    source_run_id: str,
    source_runner_sha: str,
    token: str,
    api_base: str = "https://api.github.com",
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    repository = str(repository or "").strip()
    run_id = str(source_run_id or "").strip()
    runner_sha = str(source_runner_sha or "").strip().lower()
    token = str(token or "").strip()
    if _RUN_RE.fullmatch(run_id) is None:
        raise RuntimeError("QC_PENDING resume source run id is invalid")
    if _SHA_RE.fullmatch(runner_sha) is None:
        raise RuntimeError("QC_PENDING resume source Runner SHA must be exact lowercase 40-hex")
    if not token:
        raise RuntimeError("QC_PENDING resume source gate requires GitHub token")

    repo_path = _repo_path(repository)
    base = api_base.rstrip("/")
    run = _github_json(f"{base}/repos/{repo_path}/actions/runs/{run_id}", token, opener=opener)
    if str(run.get("head_sha") or "").strip().lower() != runner_sha:
        raise RuntimeError("QC_PENDING resume source run/head SHA mismatch")
    if run.get("head_branch") != "main":
        raise RuntimeError("QC_PENDING resume source was not dispatched from main")
    if run.get("event") != "workflow_dispatch":
        raise RuntimeError("QC_PENDING resume source was not a canonical manual/Telegram production dispatch")
    if run.get("status") != "completed" or run.get("conclusion") != "failure":
        raise RuntimeError("QC_PENDING resume source run is not a completed failed production")
    if run.get("path") != _PRODUCTION_WORKFLOW:
        raise RuntimeError("QC_PENDING resume source run is not canonical Production V4")

    branch = _github_json(f"{base}/repos/{repo_path}/branches/main", token, opener=opener)
    if branch.get("protected") is not True:
        raise RuntimeError("QC_PENDING resume blocked: current main is not protected")

    compare = _github_json(
        f"{base}/repos/{repo_path}/compare/{runner_sha}...main",
        token,
        opener=opener,
    )
    if compare.get("status") not in {"ahead", "identical"}:
        raise RuntimeError("QC_PENDING resume source SHA is not an ancestor of current main")

    tags: list[str] = []
    for prefix in _REQUIRED_TAG_PREFIXES:
        tag = f"{prefix}{runner_sha}"
        payload = _github_json(
            f"{base}/repos/{repo_path}/git/ref/tags/{urllib.parse.quote(tag, safe='')}",
            token,
            opener=opener,
        )
        obj = payload.get("object") or {}
        if obj.get("type") != "commit" or obj.get("sha") != runner_sha:
            raise RuntimeError(f"QC_PENDING resume certification ref does not bind source SHA: {tag}")
        tags.append(tag)

    query = urllib.parse.urlencode(
        {
            "head_sha": runner_sha,
            "branch": "main",
            "event": "push",
            "status": "success",
            "per_page": "100",
        }
    )
    runs_payload = _github_json(
        f"{base}/repos/{repo_path}/actions/runs?{query}", token, opener=opener
    )
    runs = runs_payload.get("workflow_runs")
    if not isinstance(runs, list):
        raise RuntimeError("QC_PENDING resume certification lookup has no workflow_runs list")
    receipts: list[dict[str, Any]] = []
    for required_name, required_path in _REQUIRED_WORKFLOWS:
        match = next(
            (
                item
                for item in runs
                if isinstance(item, dict)
                and item.get("name") == required_name
                and item.get("path") == required_path
                and item.get("head_sha") == runner_sha
                and item.get("head_branch") == "main"
                and item.get("event") == "push"
                and item.get("status") == "completed"
                and item.get("conclusion") == "success"
            ),
            None,
        )
        if match is None:
            raise RuntimeError(
                f"QC_PENDING resume missing exact historical certification run: {required_name}"
            )
        receipts.append({"name": required_name, "path": required_path, "run_id": match.get("id")})

    return {
        "schema_version": 1,
        "contract_id": "gold.qc-pending.resume-source.v1",
        "status": "green",
        "repository": repository,
        "source_run_id": run_id,
        "source_runner_sha": runner_sha,
        "source_workflow": _PRODUCTION_WORKFLOW,
        "source_was_certified_protected_main": True,
        "current_main_protected": True,
        "source_is_ancestor_of_current_main": True,
        "certification_refs": tags,
        "certification_runs": receipts,
        "production_dispatch_performed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--source-run-id", required=True)
    parser.add_argument("--source-runner-sha", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = verify_historical_resume_source(
        repository=args.repository,
        source_run_id=args.source_run_id,
        source_runner_sha=args.source_runner_sha,
        token=args.token,
    )
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
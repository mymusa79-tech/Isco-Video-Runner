from __future__ import annotations

import argparse
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable


_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_REQUIRED_TAG_PREFIXES = (
    "full-regression-green-",
    "stage-ladder-green-",
)
_REQUIRED_WORKFLOWS = (
    ("Verify Private Engine", ".github/workflows/verify-private-engine.yml"),
    ("Verify Production Stage Ladder", ".github/workflows/verify-production-stage-ladder.yml"),
)


def _github_json(
    url: str,
    token: str,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "isco-production-certification-gate",
        },
    )
    try:
        with opener(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"GitHub certification lookup failed for {url}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"GitHub certification lookup returned a non-object for {url}")
    return payload


def _require_exact_successful_workflow_runs(
    *,
    base: str,
    repo_path: str,
    runner_sha: str,
    token: str,
    opener: Callable[..., Any],
) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode(
        {
            "head_sha": runner_sha,
            "branch": "main",
            "event": "push",
            "status": "success",
            "per_page": "100",
        }
    )
    payload = _github_json(
        f"{base}/repos/{repo_path}/actions/runs?{query}",
        token,
        opener=opener,
    )
    runs = payload.get("workflow_runs")
    if not isinstance(runs, list):
        raise RuntimeError("Production Fast Path blocked: Actions certification lookup has no workflow_runs list")

    verified: list[dict[str, Any]] = []
    for required_name, required_path in _REQUIRED_WORKFLOWS:
        match = next(
            (
                run
                for run in runs
                if isinstance(run, dict)
                and run.get("name") == required_name
                and run.get("path") == required_path
                and run.get("head_sha") == runner_sha
                and run.get("head_branch") == "main"
                and run.get("event") == "push"
                and run.get("status") == "completed"
                and run.get("conclusion") == "success"
            ),
            None,
        )
        if match is None:
            raise RuntimeError(
                "Production Fast Path blocked: missing exact successful main-push certification run "
                f"for {required_name} ({required_path}) at {runner_sha}"
            )
        verified.append(
            {
                "name": required_name,
                "path": required_path,
                "run_id": match.get("id"),
                "head_sha": runner_sha,
                "event": "push",
                "conclusion": "success",
            }
        )
    return verified


def verify_certified_production_source(
    *,
    repository: str,
    runner_sha: str,
    git_ref: str,
    token: str,
    api_base: str = "https://api.github.com",
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    repository = str(repository or "").strip()
    runner_sha = str(runner_sha or "").strip()
    git_ref = str(git_ref or "").strip()
    token = str(token or "").strip()

    if not repository or repository.count("/") != 1:
        raise RuntimeError("GITHUB_REPOSITORY must be owner/repository")
    if not _SHA_RE.fullmatch(runner_sha):
        raise RuntimeError("GITHUB_SHA must be an exact lowercase 40-character SHA")
    if git_ref != "refs/heads/main":
        raise RuntimeError(f"Production Fast Path is main-only, got {git_ref!r}")
    if not token:
        raise RuntimeError("GITHUB_TOKEN is required for certification verification")

    repo_path = "/".join(urllib.parse.quote(part, safe="") for part in repository.split("/"))
    base = api_base.rstrip("/")
    branch = _github_json(f"{base}/repos/{repo_path}/branches/main", token, opener=opener)
    if branch.get("protected") is not True:
        raise RuntimeError("Production Fast Path blocked: main is not protected")
    branch_sha = str((branch.get("commit") or {}).get("sha") or "").strip()
    if branch_sha != runner_sha:
        raise RuntimeError(
            f"Production Fast Path blocked: dispatched SHA {runner_sha} is not current main {branch_sha}"
        )

    verified_tags: list[str] = []
    for prefix in _REQUIRED_TAG_PREFIXES:
        tag = f"{prefix}{runner_sha}"
        encoded_tag = urllib.parse.quote(tag, safe="")
        payload = _github_json(
            f"{base}/repos/{repo_path}/git/ref/tags/{encoded_tag}",
            token,
            opener=opener,
        )
        obj = payload.get("object") or {}
        if obj.get("type") != "commit" or obj.get("sha") != runner_sha:
            raise RuntimeError(
                f"Production Fast Path blocked: certification ref {tag} does not bind exact Runner SHA"
            )
        verified_tags.append(tag)

    verified_runs = _require_exact_successful_workflow_runs(
        base=base,
        repo_path=repo_path,
        runner_sha=runner_sha,
        token=token,
        opener=opener,
    )

    return {
        "schema": "isco-production-certification-gate-v2",
        "repository": repository,
        "runner_sha": runner_sha,
        "main_protected": True,
        "certification_refs": verified_tags,
        "certification_runs": verified_runs,
        "status": "green",
        "production_dispatch_performed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Fail-closed exact-SHA Production certification gate")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    result = verify_certified_production_source(
        repository=os.environ.get("GITHUB_REPOSITORY", ""),
        runner_sha=os.environ.get("GITHUB_SHA", ""),
        git_ref=os.environ.get("GITHUB_REF", ""),
        token=os.environ.get("GITHUB_TOKEN", ""),
    )
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

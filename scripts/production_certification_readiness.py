from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

# This module is imported by certification-resume code and is also executed
# directly by the Telegram production workflow. Direct execution of a file
# under scripts/ makes sys.path[0] point at scripts/ rather than the repository
# root, which breaks absolute imports from the scripts package. Bootstrap only
# that direct-CLI case so package imports and `python -m` execution are unchanged.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.production_certification_gate import (
    _REQUIRED_TAG_PREFIXES,
    _REQUIRED_WORKFLOWS,
    _SHA_RE,
)


def _request_json(
    url: str,
    token: str,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
    missing_ok: bool = False,
) -> dict[str, Any] | None:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "isco-production-certification-readiness",
        },
    )
    try:
        with opener(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if missing_ok and exc.code == 404:
            return None
        raise RuntimeError(f"GitHub certification readiness lookup failed for {url}: {exc}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"GitHub certification readiness lookup failed for {url}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"GitHub certification readiness lookup returned a non-object for {url}")
    return payload


def _matching_runs(runs: list[Any], *, name: str, path: str, runner_sha: str) -> list[dict[str, Any]]:
    return [
        run
        for run in runs
        if isinstance(run, dict)
        and run.get("name") == name
        and run.get("path") == path
        and run.get("head_sha") == runner_sha
        and run.get("head_branch") == "main"
        and run.get("event") == "push"
    ]


def inspect_production_certification_readiness(
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
        raise RuntimeError(f"Production certification readiness is main-only, got {git_ref!r}")
    if not token:
        raise RuntimeError("GITHUB_TOKEN is required for certification readiness verification")

    repo_path = "/".join(urllib.parse.quote(part, safe="") for part in repository.split("/"))
    base = api_base.rstrip("/")
    branch = _request_json(f"{base}/repos/{repo_path}/branches/main", token, opener=opener)
    assert branch is not None
    if branch.get("protected") is not True:
        return {
            "schema": "isco-production-certification-readiness-v1",
            "repository": repository,
            "runner_sha": runner_sha,
            "status": "failed",
            "reason": "main_not_protected",
            "production_dispatch_performed": False,
        }
    branch_sha = str((branch.get("commit") or {}).get("sha") or "").strip()
    if branch_sha != runner_sha:
        return {
            "schema": "isco-production-certification-readiness-v1",
            "repository": repository,
            "runner_sha": runner_sha,
            "current_main_sha": branch_sha,
            "status": "stale",
            "reason": "main_advanced",
            "production_dispatch_performed": False,
        }

    query = urllib.parse.urlencode(
        {
            "head_sha": runner_sha,
            "branch": "main",
            "event": "push",
            "per_page": "100",
        }
    )
    actions = _request_json(
        f"{base}/repos/{repo_path}/actions/runs?{query}",
        token,
        opener=opener,
    )
    assert actions is not None
    runs = actions.get("workflow_runs")
    if not isinstance(runs, list):
        raise RuntimeError("Production certification readiness lookup has no workflow_runs list")

    workflow_evidence: list[dict[str, Any]] = []
    terminal_failure = False
    workflow_pending = False
    for name, path in _REQUIRED_WORKFLOWS:
        matches = _matching_runs(runs, name=name, path=path, runner_sha=runner_sha)
        success = next(
            (
                run
                for run in matches
                if run.get("status") == "completed" and run.get("conclusion") == "success"
            ),
            None,
        )
        active = next((run for run in matches if run.get("status") != "completed"), None)
        if success is not None:
            state = "ready"
            selected = success
        elif active is not None:
            state = "pending"
            selected = active
            workflow_pending = True
        elif matches:
            state = "failed"
            selected = matches[0]
            terminal_failure = True
        else:
            state = "pending"
            selected = {}
            workflow_pending = True
        workflow_evidence.append(
            {
                "name": name,
                "path": path,
                "state": state,
                "run_id": selected.get("id"),
                "run_status": selected.get("status"),
                "run_conclusion": selected.get("conclusion"),
            }
        )

    tag_evidence: list[dict[str, Any]] = []
    tag_pending = False
    tag_failure = False
    all_workflows_ready = all(item["state"] == "ready" for item in workflow_evidence)
    for prefix in _REQUIRED_TAG_PREFIXES:
        tag = f"{prefix}{runner_sha}"
        encoded_tag = urllib.parse.quote(tag, safe="")
        payload = _request_json(
            f"{base}/repos/{repo_path}/git/ref/tags/{encoded_tag}",
            token,
            opener=opener,
            missing_ok=True,
        )
        if payload is None:
            state = "failed" if all_workflows_ready else "pending"
            tag_failure = tag_failure or state == "failed"
            tag_pending = tag_pending or state == "pending"
            tag_evidence.append({"tag": tag, "state": state, "reason": "missing"})
            continue
        obj = payload.get("object") or {}
        if obj.get("type") != "commit" or obj.get("sha") != runner_sha:
            tag_failure = True
            tag_evidence.append(
                {
                    "tag": tag,
                    "state": "failed",
                    "reason": "identity_mismatch",
                    "object_type": obj.get("type"),
                    "object_sha": obj.get("sha"),
                }
            )
        else:
            tag_evidence.append({"tag": tag, "state": "ready", "object_sha": runner_sha})

    if terminal_failure or tag_failure:
        status = "failed"
        reason = "certification_failed"
    elif workflow_pending or tag_pending:
        status = "pending"
        reason = "certification_in_progress"
    else:
        status = "ready"
        reason = "exact_sha_certified"

    return {
        "schema": "isco-production-certification-readiness-v1",
        "repository": repository,
        "runner_sha": runner_sha,
        "current_main_sha": branch_sha,
        "main_protected": True,
        "status": status,
        "reason": reason,
        "workflows": workflow_evidence,
        "certification_refs": tag_evidence,
        "production_dispatch_performed": False,
    }


def wait_for_production_certification_readiness(
    *,
    repository: str,
    runner_sha: str,
    git_ref: str,
    token: str,
    max_attempts: int = 25,
    poll_seconds: float = 5.0,
    api_base: str = "https://api.github.com",
    opener: Callable[..., Any] = urllib.request.urlopen,
    sleeper: Callable[[float], Any] = time.sleep,
) -> dict[str, Any]:
    if int(max_attempts) < 1:
        raise RuntimeError("max_attempts must be at least 1")
    if float(poll_seconds) < 0:
        raise RuntimeError("poll_seconds cannot be negative")

    result: dict[str, Any] = {}
    for attempt in range(1, int(max_attempts) + 1):
        result = inspect_production_certification_readiness(
            repository=repository,
            runner_sha=runner_sha,
            git_ref=git_ref,
            token=token,
            api_base=api_base,
            opener=opener,
        )
        result["attempts"] = attempt
        result["wait_exhausted"] = False
        if result["status"] != "pending":
            return result
        if attempt < int(max_attempts):
            sleeper(float(poll_seconds))
    result["wait_exhausted"] = True
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Bounded exact-SHA production certification readiness wait")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-attempts", type=int, default=25)
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    args = parser.parse_args()

    result = wait_for_production_certification_readiness(
        repository=os.environ.get("GITHUB_REPOSITORY", ""),
        runner_sha=os.environ.get("GITHUB_SHA", ""),
        git_ref=os.environ.get("GITHUB_REF", ""),
        token=os.environ.get("GITHUB_TOKEN", ""),
        max_attempts=args.max_attempts,
        poll_seconds=args.poll_seconds,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True))
    if result["status"] == "ready":
        return 0
    if result["status"] == "pending":
        return 3
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

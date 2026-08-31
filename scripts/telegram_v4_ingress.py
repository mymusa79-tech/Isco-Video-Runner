from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

# This file is invoked both as a module and directly by GitHub Actions. Direct
# execution sets sys.path[0] to scripts/, which otherwise makes `import scripts.*`
# fail during terminal reconciliation. Anchor imports to the checked-out Runner.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.telegram_production_queue import (
    consume_dispatch_authorization,
    mark_dispatch_completed,
    mark_dispatch_failed,
    release_tag_for,
    validate_dispatch_authorization,
    validate_ready_request,
)


def _load(path: Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("Telegram control state must be an object")
    return data


def _save(path: Path, state: dict[str, Any]) -> None:
    Path(path).write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _request(state: dict[str, Any], request_id: str, request_sha256: str) -> dict[str, Any]:
    requests = state.get("requests")
    if not isinstance(requests, dict):
        raise RuntimeError("Telegram control state has no request registry")
    request = requests.get(str(request_id or ""))
    if not isinstance(request, dict):
        raise RuntimeError("Approved Telegram request id is absent from encrypted state")
    validate_ready_request(request)
    if str(request.get("request_sha256") or "") != str(request_sha256 or ""):
        raise RuntimeError("Approved Telegram request hash does not match workflow dispatch")
    return request


def _github_output(path: Path | None, **values: object) -> None:
    if path is None:
        return
    with Path(path).open("a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def _current_runner_sha() -> str:
    value = str(os.environ.get("GITHUB_SHA") or "").strip().lower()
    if len(value) != 40 or any(ch not in "0123456789abcdef" for ch in value):
        raise RuntimeError("V4 Telegram ingress requires exact current GITHUB_SHA")
    return value


def prepare(
    *,
    state_path: Path,
    request_id: str,
    request_sha256: str,
    authorization_id: str,
    engine_sha: str,
    expected_engine_sha: str,
    approved_request_output: Path,
    brief_output: Path,
    request_output: Path,
    workflow_run_id: str,
    github_output: Path | None = None,
) -> dict[str, str]:
    if str(engine_sha or "").strip() != str(expected_engine_sha or "").strip():
        raise RuntimeError("Telegram dispatch Engine SHA does not match certified V4 Engine pin")
    runner_sha = _current_runner_sha()
    state = _load(state_path)
    request = _request(state, request_id, request_sha256)
    validate_dispatch_authorization(
        state,
        request_id,
        request_sha256,
        authorization_id,
        runner_sha=runner_sha,
    )

    approved_request_output = Path(approved_request_output)
    approved_request_output.parent.mkdir(parents=True, exist_ok=True)
    approved_request_output.write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
    approved_request_output.chmod(0o600)

    # Only V4 preparation needs Engine code. Admission/rejection/completion state
    # transitions remain dependency-light so the Telegram gateway never installs or
    # imports the production Engine merely to release a reservation.
    from scripts.control_approved_brief import materialize_approved_brief

    brief_path, brief_sha256 = materialize_approved_brief(request, Path(brief_output))
    brief_path.chmod(0o600)
    fmt = "moment" if request.get("kind") == "short" else str(request.get("format") or "film")
    request_output = Path(request_output)
    request_output.parent.mkdir(parents=True, exist_ok=True)
    request_output.write_text(
        json.dumps({"topic": str(request.get("approved_topic") or "").strip(), "format": fmt}, ensure_ascii=False),
        encoding="utf-8",
    )
    request_output.chmod(0o600)

    release_tag = release_tag_for(request)
    consume_dispatch_authorization(
        state,
        request_id,
        request_sha256,
        authorization_id,
        workflow_run_id=workflow_run_id,
        runner_sha=runner_sha,
    )
    _save(state_path, state)
    values = {
        "brief_sha256": brief_sha256,
        "release_tag": release_tag,
        "kind": str(request.get("kind") or ""),
        "approval_scope": str(request.get("approval_scope") or ""),
        "runner_sha": runner_sha,
    }
    _github_output(github_output, **values)
    return values


def complete(
    *,
    state_path: Path,
    request_id: str,
    request_sha256: str,
    authorization_id: str,
    release_tag: str,
) -> None:
    state = _load(state_path)
    request = _request(state, request_id, request_sha256)
    mark_dispatch_completed(
        state,
        request_id,
        request_sha256,
        authorization_id,
        release_tag=release_tag,
    )
    from scripts.telegram_control_active_ui import _mark_request_used

    _mark_request_used(state, request, release_tag=release_tag)
    _save(state_path, state)


def fail(
    *,
    state_path: Path,
    request_id: str,
    request_sha256: str,
    authorization_id: str,
    reason: str,
) -> None:
    state = _load(state_path)
    mark_dispatch_failed(
        state,
        request_id,
        request_sha256,
        authorization_id,
        reason=reason,
    )
    _save(state_path, state)


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    prepare_cmd = sub.add_parser("prepare")
    prepare_cmd.add_argument("--state", required=True, type=Path)
    prepare_cmd.add_argument("--request-id", required=True)
    prepare_cmd.add_argument("--sha256", required=True)
    prepare_cmd.add_argument("--authorization-id", required=True)
    prepare_cmd.add_argument("--engine-sha", required=True)
    prepare_cmd.add_argument("--expected-engine-sha", required=True)
    prepare_cmd.add_argument("--approved-request-output", required=True, type=Path)
    prepare_cmd.add_argument("--brief-output", required=True, type=Path)
    prepare_cmd.add_argument("--request-output", required=True, type=Path)
    prepare_cmd.add_argument("--workflow-run-id", default="")
    prepare_cmd.add_argument("--github-output", type=Path)

    complete_cmd = sub.add_parser("complete")
    complete_cmd.add_argument("--state", required=True, type=Path)
    complete_cmd.add_argument("--request-id", required=True)
    complete_cmd.add_argument("--sha256", required=True)
    complete_cmd.add_argument("--authorization-id", required=True)
    complete_cmd.add_argument("--release-tag", required=True)

    fail_cmd = sub.add_parser("fail")
    fail_cmd.add_argument("--state", required=True, type=Path)
    fail_cmd.add_argument("--request-id", required=True)
    fail_cmd.add_argument("--sha256", required=True)
    fail_cmd.add_argument("--authorization-id", required=True)
    fail_cmd.add_argument(
        "--reason",
        required=True,
        choices=("workflow_dispatch_failed", "production_failed", "production_cancelled"),
    )

    args = parser.parse_args()
    if args.command == "prepare":
        prepare(
            state_path=args.state,
            request_id=args.request_id,
            request_sha256=args.sha256,
            authorization_id=args.authorization_id,
            engine_sha=args.engine_sha,
            expected_engine_sha=args.expected_engine_sha,
            approved_request_output=args.approved_request_output,
            brief_output=args.brief_output,
            request_output=args.request_output,
            workflow_run_id=args.workflow_run_id,
            github_output=args.github_output,
        )
    elif args.command == "complete":
        complete(
            state_path=args.state,
            request_id=args.request_id,
            request_sha256=args.sha256,
            authorization_id=args.authorization_id,
            release_tag=args.release_tag,
        )
    else:
        fail(
            state_path=args.state,
            request_id=args.request_id,
            request_sha256=args.sha256,
            authorization_id=args.authorization_id,
            reason=args.reason,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

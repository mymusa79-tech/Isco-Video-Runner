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
if __package__ in {None, ""}:
    _REPO_ROOT = Path(__file__).resolve().parent.parent
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))

from scripts.telegram_production_queue import (
    consume_dispatch_authorization,
    mark_dispatch_completed,
    mark_dispatch_failed,
    mark_dispatch_qc_pending,
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


def _github_env(**values: object) -> None:
    path = str(os.environ.get("GITHUB_ENV") or "").strip()
    if not path:
        return
    with Path(path).open("a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def _current_runner_sha() -> str:
    value = str(os.environ.get("GITHUB_SHA") or "").strip().lower()
    if len(value) != 40 or any(ch not in "0123456789abcdef" for ch in value):
        raise RuntimeError("V4 Telegram ingress requires exact current GITHUB_SHA")
    return value


def _current_qc_pending_recovery() -> tuple[Path, str] | None:
    """Return one exact current-run recovery bundle, never a text-matched failure.

    Canonical V4 uploads `short-*` output paths in its existing failure diagnostics.
    The QC_PENDING capture writes a verified bundle under that allowlist before the
    process unwinds. Terminal reconciliation may promote the dispatch only when that
    exact bundle revalidates against current run/Runner/Engine provenance.

    Gold/Final-Master modules stay lazy here so normal prepare/complete/fail paths keep
    the historical dependency-light Telegram ingress boundary.
    """
    run_id = str(os.environ.get("GITHUB_RUN_ID") or "").strip()
    run_attempt = str(os.environ.get("GITHUB_RUN_ATTEMPT") or "").strip()
    run_number = str(os.environ.get("GITHUB_RUN_NUMBER") or "").strip()
    runner_sha = _current_runner_sha()
    if not run_id.isdigit() or int(run_id) < 1:
        return None
    if not run_attempt.isdigit() or int(run_attempt) < 1:
        return None
    if not run_number.isdigit() or int(run_number) < 1:
        return None

    output_parent = Path("engine") / "output"
    if not output_parent.is_dir():
        return None

    from scripts.qc_pending_checkpoint_v1 import RECOVERY_BUNDLE_DIRNAME
    from scripts.qc_pending_resume_bundle_v1 import validate_resume_bundle

    matches: list[Path] = []
    for bundle in output_parent.glob(f"*/{RECOVERY_BUNDLE_DIRNAME}"):
        if not bundle.is_dir():
            continue
        try:
            manifest = validate_resume_bundle(
                bundle,
                expected_source_run_id=run_id,
                expected_runner_sha=runner_sha,
            )
        except RuntimeError:
            continue
        source = manifest.get("source") or {}
        if str(source.get("run_attempt") or "").strip() != run_attempt:
            continue
        checkpoint = bundle.parent / "qc-pending.json"
        if not checkpoint.is_file():
            continue
        matches.append(checkpoint)
    if not matches:
        return None
    if len(matches) != 1:
        raise RuntimeError("Multiple exact QC_PENDING recovery bundles matched one production run")

    # This is a logical locator accepted by the production ledger. The Gold-resume
    # queue resolves it to the already-uploaded diagnostics artifact name; no second
    # artifact upload or production retry is needed.
    artifact_locator = f"isco-qc-pending-diagnostics-{run_number}"
    return matches[0], artifact_locator


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

    # The runtime request must use the exact same outer format that was resolved and
    # cryptographically bound into the Approved Brief. Never re-read the untrusted
    # pre-resolution `request["format"]` here: new Long approvals intentionally store
    # `auto` until this V4 seam, while Planning must receive the final film/story.
    bound_brief = json.loads(brief_path.read_text(encoding="utf-8"))
    fmt = str(bound_brief.get("format") or "").strip()
    expected_formats = {"moment"} if request.get("kind") == "short" else {"film", "story"}
    if fmt not in expected_formats:
        raise RuntimeError("Resolved Approved Brief format is inconsistent with Telegram request kind")

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
        "resolved_format": fmt,
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


def qc_pending(
    *,
    state_path: Path,
    request_id: str,
    request_sha256: str,
    authorization_id: str,
    checkpoint_path: Path,
    artifact_name: str,
) -> None:
    """Reconcile an exact post-render Gold-capacity pause without classifying it as failure."""
    state = _load(state_path)
    _request(state, request_id, request_sha256)
    checkpoint = _load(Path(checkpoint_path))
    if checkpoint.get("contract_id") != "gold.qc-pending.v1" or checkpoint.get("schema_version") != 2:
        raise RuntimeError("Telegram QC_PENDING reconciliation requires checkpoint V2")
    if checkpoint.get("status") != "GOLD_VISION_PENDING_PROVIDER_CAPACITY":
        raise RuntimeError("Telegram QC_PENDING reconciliation received a non-pending checkpoint")
    if checkpoint.get("release_allowed") is not False or checkpoint.get("resumable") is not True:
        raise RuntimeError("Telegram QC_PENDING checkpoint is not fail-closed resumable evidence")
    if checkpoint.get("failure_taxonomy") != "VisionProviderMeshUnavailableError":
        raise RuntimeError("Telegram QC_PENDING reconciliation refuses unsupported failure taxonomy")
    runner_sha = str(checkpoint.get("runner_sha") or "").strip().lower()
    if runner_sha != _current_runner_sha():
        raise RuntimeError("Telegram QC_PENDING checkpoint Runner SHA does not match source workflow")
    source_run_id = str(checkpoint.get("source_run_id") or "").strip()
    source_run_attempt = str(checkpoint.get("source_run_attempt") or "").strip()
    if source_run_id != str(os.environ.get("GITHUB_RUN_ID") or "").strip():
        raise RuntimeError("Telegram QC_PENDING checkpoint run id does not match source workflow")
    if source_run_attempt != str(os.environ.get("GITHUB_RUN_ATTEMPT") or "").strip():
        raise RuntimeError("Telegram QC_PENDING checkpoint attempt does not match source workflow")
    final_sha256 = str((checkpoint.get("final") or {}).get("sha256") or "").strip().lower()
    mark_dispatch_qc_pending(
        state,
        request_id,
        request_sha256,
        authorization_id,
        source_run_id=source_run_id,
        source_run_attempt=source_run_attempt,
        artifact_name=artifact_name,
        runner_sha=runner_sha,
        engine_sha=str(checkpoint.get("engine_sha") or "").strip().lower(),
        final_sha256=final_sha256,
        fmt=str(checkpoint.get("format") or "").strip().lower(),
    )
    _save(state_path, state)


def fail(
    *,
    state_path: Path,
    request_id: str,
    request_sha256: str,
    authorization_id: str,
    reason: str,
) -> None:
    if reason == "production_failed":
        recovery = _current_qc_pending_recovery()
        if recovery is not None:
            checkpoint_path, artifact_locator = recovery
            qc_pending(
                state_path=state_path,
                request_id=request_id,
                request_sha256=request_sha256,
                authorization_id=authorization_id,
                checkpoint_path=checkpoint_path,
                artifact_name=artifact_locator,
            )
            _github_env(
                ISCO_QC_PENDING="true",
                ISCO_QC_PENDING_REQUEST_ID=request_id,
                ISCO_QC_PENDING_ARTIFACT_LOCATOR=artifact_locator,
            )
            print(
                "Telegram terminal reconciliation: exact Gold QC_PENDING promoted before generic failure; "
                f"checkpoint={checkpoint_path} artifact_locator={artifact_locator}"
            )
            return

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

    pending_cmd = sub.add_parser("qc-pending")
    pending_cmd.add_argument("--state", required=True, type=Path)
    pending_cmd.add_argument("--request-id", required=True)
    pending_cmd.add_argument("--sha256", required=True)
    pending_cmd.add_argument("--authorization-id", required=True)
    pending_cmd.add_argument("--checkpoint", required=True, type=Path)
    pending_cmd.add_argument("--artifact-name", required=True)

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
    elif args.command == "qc-pending":
        qc_pending(
            state_path=args.state,
            request_id=args.request_id,
            request_sha256=args.sha256,
            authorization_id=args.authorization_id,
            checkpoint_path=args.checkpoint,
            artifact_name=args.artifact_name,
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

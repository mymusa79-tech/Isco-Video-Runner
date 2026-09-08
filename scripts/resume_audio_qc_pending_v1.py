from __future__ import annotations

"""Resume an AUDIO_QC_PENDING parent without replaying production.

The source render is immutable.  The executor revalidates only the missing Audio
Production V2 auditor evidence, then sends the same bytes through the already-certified
Producer Handoff -> Audio Semantic Integrity -> Final Master QC -> Gold/Viewer chain.
Planning, research, stock retrieval, TTS and parent rendering have no entrypoints here.
"""

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Iterator

import isco_video_agent.orchestrator as orchestrator
from isco_video_agent.ai_budget import BudgetLedger
from isco_video_agent.config import secret
from isco_video_agent.production_pipeline import _output_key

import scripts.audio_producer_final_certificate as producer_certificate
import scripts.run_v3_voice as production
from scripts.audio_production_resume_v1 import (
    require_existing_audio_production_pass,
    resume_audio_production_contract_v2,
)
from scripts.audio_qc_pending_resume_bundle_v1 import validate_resume_bundle
from scripts.audio_semantic_resume_state_v1 import restore_audio_semantic_resume_state
from scripts.finalize_gold_resume_delivery_v1 import _write_resume_production_manifest
from scripts.gold_enforce_phase4 import run_gold_enforce_phase4
from scripts.gold_vision_capacity_reserve_v1 import install_gold_vision_capacity_reserve_v1
from scripts.production_model_contract import install_production_model_contract
from scripts.resume_gold_qc_pending_v1 import _prepare_temporary_history, _verify_accepted_history
from scripts.runtime_closure import install_runtime_closure, run_post_gold_observers


CONTRACT_ID = "audio.qc-pending.resume-execution.v1"


class AudioQCPendingResumeError(RuntimeError):
    pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise AudioQCPendingResumeError(f"audio_resume_invalid_json:{Path(path).name}") from exc
    if not isinstance(value, dict):
        raise AudioQCPendingResumeError(f"audio_resume_wrong_shape:{Path(path).name}")
    return value


def _git_head(root: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout.strip()


@contextlib.contextmanager
def _reuse_resumed_audio_pass_in_final_chain() -> Iterator[None]:
    """Prevent a second provider call after the bounded resume already reached PASS."""
    original = producer_certificate.require_audio_production_contract_v2
    producer_certificate.require_audio_production_contract_v2 = require_existing_audio_production_pass
    try:
        yield
    finally:
        producer_certificate.require_audio_production_contract_v2 = original


def _materialize_control_request(checkpoint: dict[str, Any], directory: Path) -> Path | None:
    request = checkpoint.get("control_request")
    ingress = str(checkpoint.get("ingress") or "").strip()
    if ingress == "manual":
        if request is not None:
            raise AudioQCPendingResumeError("audio_resume_manual_checkpoint_contains_control_request")
        return None
    if ingress != "telegram" or not isinstance(request, dict):
        raise AudioQCPendingResumeError("audio_resume_telegram_control_request_missing")
    if request.get("approved_by_user") is not True:
        raise AudioQCPendingResumeError("audio_resume_control_request_not_user_approved")
    path = Path(directory) / "approved-control-request.json"
    path.write_text(json.dumps(request, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def execute_audio_resume(
    *,
    bundle_dir: Path,
    durable_history: Path,
    accepted_history_output: Path,
    result_output: Path,
    expected_source_run_id: str,
    expected_source_runner_sha: str,
    expected_source_engine_sha: str,
    expected_runtime_runner_sha: str,
    expected_runtime_engine_sha: str,
) -> dict[str, Any]:
    root = Path(bundle_dir).resolve()
    manifest = validate_resume_bundle(
        root,
        expected_source_run_id=expected_source_run_id,
        expected_runner_sha=expected_source_runner_sha,
        expected_engine_sha=expected_source_engine_sha,
    )
    source = manifest["source"]
    checkpoint = _read_object(root / "audio-qc-pending.json")
    retry_policy = checkpoint.get("retry_policy") or {}
    if int(retry_policy.get("resume_execution_limit") or 0) != 1:
        raise AudioQCPendingResumeError("audio_resume_execution_limit_contract_invalid")
    if retry_policy.get("semantic_mismatch_is_resumable") is not False:
        raise AudioQCPendingResumeError("audio_resume_semantic_mismatch_policy_invalid")

    runner_root = Path(__file__).resolve().parents[1]
    engine_root = Path(orchestrator.__file__).resolve().parents[2]
    runtime_runner_head = _git_head(runner_root)
    runtime_engine_head = _git_head(engine_root)
    if runtime_runner_head != expected_runtime_runner_sha:
        raise AudioQCPendingResumeError("audio_resume_runtime_runner_not_certified_sha")
    if runtime_engine_head != expected_runtime_engine_sha:
        raise AudioQCPendingResumeError("audio_resume_runtime_engine_not_certified_sha")
    if str(os.environ.get("GITHUB_SHA") or "").strip().lower() != expected_runtime_runner_sha:
        raise AudioQCPendingResumeError("audio_resume_github_sha_not_runtime_runner")

    state = checkpoint.get("production_state") or {}
    output_key = str(state.get("output_key") or "").strip()
    pending_record = state.get("record")
    if not output_key or not isinstance(pending_record, dict):
        raise AudioQCPendingResumeError("audio_resume_pending_production_state_missing")
    if _output_key(root) != output_key:
        raise AudioQCPendingResumeError("audio_resume_bundle_not_restored_at_original_output_key")

    final_path = root / "final.mp4"
    final_sha_before = _sha256_file(final_path)
    if final_sha_before != str(manifest.get("final_sha256") or "").strip().lower():
        raise AudioQCPendingResumeError("audio_resume_parent_final_identity_changed")

    source_run_id = str(source.get("run_id") or "")
    source_attempt = str(source.get("run_attempt") or "")
    source_production_id = str(source.get("production_id") or "")
    if source_production_id != f"v4:{source_run_id}:{source_attempt}":
        raise AudioQCPendingResumeError("audio_resume_source_production_identity_invalid")

    temporary_history = accepted_history_output.with_name(accepted_history_output.name + ".working")
    _prepare_temporary_history(
        durable_history=durable_history,
        temporary_history=temporary_history,
        pending_record=pending_record,
        output_key=output_key,
    )

    os.environ["ISCO_HISTORY_PATH"] = str(temporary_history)
    os.environ["ISCO_ENGINE_SHA"] = expected_runtime_engine_sha
    os.environ["ISCO_SOURCE_RUNNER_SHA"] = expected_source_runner_sha
    os.environ["ISCO_SOURCE_ENGINE_SHA"] = expected_source_engine_sha
    os.environ["ISCO_SOURCE_RUN_ID"] = source_run_id
    os.environ["ISCO_SOURCE_RUN_ATTEMPT"] = source_attempt
    os.environ["ISCO_SOURCE_PRODUCTION_ID"] = source_production_id
    os.environ["ISCO_PRODUCTION_ID"] = source_production_id
    os.environ["ISCO_AI_BUDGET_ENFORCE"] = "1"

    # No produce() call occurs.  Runtime Closure is installed only to reconstruct the
    # same certified post-render wrapper topology that the source run would have used.
    install_production_model_contract(orchestrator)
    install_runtime_closure()
    install_gold_vision_capacity_reserve_v1()
    restore_audio_semantic_resume_state(
        root,
        expected_production_id=source_production_id,
    )

    gemini = secret("GEMINI_API_KEY")
    pexels = secret("PEXELS_API_KEY")
    pixabay = secret("PIXABAY_API_KEY")
    if not gemini or not pexels:
        raise AudioQCPendingResumeError("audio_resume_requires_gemini_and_pexels_credentials")

    # Retry only the failed independent auditor (or both if neither produced semantic
    # evidence), then force the normal wrapper chain to *validate* that PASS without a
    # second provider request.
    resumed_audio = resume_audio_production_contract_v2(root)
    if resumed_audio.get("decision") != "pass":
        raise AudioQCPendingResumeError("audio_resume_audio_contract_returned_without_pass")
    final_sha_after_audio = _sha256_file(final_path)
    if final_sha_after_audio != final_sha_before:
        raise AudioQCPendingResumeError("audio_resume_audio_revalidation_mutated_parent")

    with _reuse_resumed_audio_pass_in_final_chain():
        master_qc = production.run_final_master_qc(root)
    if (
        master_qc.get("status") != "pass"
        or master_qc.get("final_media_mutated") is not False
        or _sha256_file(final_path) != final_sha_before
    ):
        raise AudioQCPendingResumeError("audio_resume_final_master_revalidation_failed")

    fmt = str(checkpoint.get("format") or "").strip().lower()
    if fmt not in {"film", "story", "moment"}:
        raise AudioQCPendingResumeError("audio_resume_format_invalid")
    ledger = BudgetLedger(fmt, enforce=True)
    plan, critic, gold_report = run_gold_enforce_phase4(
        output_dir=root,
        gemini=gemini,
        pexels=pexels,
        pixabay=pixabay,
        ledger=ledger,
    )
    if _sha256_file(final_path) != final_sha_before:
        raise AudioQCPendingResumeError("audio_resume_gold_mutated_parent")
    if gold_report.get("gold", {}).get("accepted") is not True:
        raise AudioQCPendingResumeError("audio_resume_gold_not_accepted")
    if gold_report.get("viewer_quality", {}).get("verdict") != "pass":
        raise AudioQCPendingResumeError("audio_resume_viewer_quality_not_pass")

    accepted_row = _verify_accepted_history(temporary_history, output_key=output_key)
    accepted_history_output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(temporary_history, accepted_history_output)
    accepted_history_output.chmod(0o600)
    ledger.write(root / "ai-budget-audio-resume.json")

    release_tag = str(manifest.get("release_tag") or checkpoint.get("release_tag") or "").strip()
    if not release_tag:
        raise AudioQCPendingResumeError("audio_resume_release_tag_missing")
    control_request_path: Path | None
    with tempfile.TemporaryDirectory(prefix="isco-audio-resume-control-") as temp_dir:
        control_request_path = _materialize_control_request(checkpoint, Path(temp_dir))
        if control_request_path is None:
            # Manual production uses the same post-manifest canonical bundle wrapper as
            # the normal V4 path.  Explicit activation is scoped to this already-Gold
            # continuation and never enables orchestrator.produce().
            os.environ["ISCO_CANONICAL_V4_BUNDLE_ENABLED"] = "1"
            os.environ.pop("ISCO_CONTROL_REQUEST_ID", None)
            run_post_gold_observers(root)
            production_manifest = _write_resume_production_manifest(
                root,
                fmt=fmt,
                release_tag=release_tag,
                source=source,
            )
            delivery_manifest = root / "delivery-manifest.json"
            delivery_path = str(delivery_manifest) if delivery_manifest.is_file() else None
            continuation = {
                "status": "manual_delivery_staged" if delivery_path else "manual_release_evidence_ready",
                "production_manifest": str(root / "production-manifest.json"),
                "delivery_manifest": delivery_path,
            }
        else:
            from scripts.finalize_gold_resume_delivery_v1 import finalize_after_gold_resume

            os.environ["ISCO_CONTROL_REQUEST_ID"] = str(checkpoint["control_request"].get("request_id") or "")
            continuation_result = root / "audio-resume-delivery-result.json"
            continuation = finalize_after_gold_resume(
                output_dir=root,
                request_path=control_request_path,
                resume_manifest_path=root / "resume-manifest.json",
                release_tag=release_tag,
                runtime_root=runner_root,
                result_output=continuation_result,
            )
            production_manifest = _read_object(root / "production-manifest.json")

    if _sha256_file(final_path) != final_sha_before:
        raise AudioQCPendingResumeError("audio_resume_post_gold_continuation_mutated_parent")

    result = {
        "schema_version": 1,
        "contract_id": CONTRACT_ID,
        "status": "gold_accepted_delivery_staged",
        "release_authority": "gold",
        "source_run_id": source_run_id,
        "source_run_attempt": source_attempt,
        "source_runner_sha": expected_source_runner_sha,
        "source_engine_sha": expected_source_engine_sha,
        "runtime_runner_sha": expected_runtime_runner_sha,
        "runtime_engine_sha": expected_runtime_engine_sha,
        "source_and_runtime_both_certified": True,
        "format": str(getattr(plan, "format", fmt)),
        "final_sha256_before": final_sha_before,
        "final_sha256_after": _sha256_file(final_path),
        "final_media_mutated": False,
        "audio_resume_provider_attempts": int(resumed_audio.get("resume_provider_attempts") or 0),
        "audio_contract_decision": resumed_audio.get("decision"),
        "final_master_qc_reexecuted": True,
        "viewer_score_10": gold_report.get("viewer_quality", {}).get("score_10"),
        "production_history_release_status": accepted_row.get("release_status"),
        "planning_performed": False,
        "research_performed": False,
        "visual_retrieval_performed": False,
        "tts_performed": False,
        "parent_rerender_performed": False,
        "durable_history_persisted_by_executor": False,
        "durable_history_ready_for_persistence": True,
        "critic_status": critic.get("status") if isinstance(critic, dict) else None,
        "production_manifest_release_authority": production_manifest.get("release_authority"),
        "continuation": continuation,
    }
    result_output.parent.mkdir(parents=True, exist_ok=True)
    result_output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        temporary_history.unlink()
    except OSError:
        pass
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--durable-history", required=True, type=Path)
    parser.add_argument("--accepted-history-output", required=True, type=Path)
    parser.add_argument("--result", required=True, type=Path)
    parser.add_argument("--source-run-id", required=True)
    parser.add_argument("--source-runner-sha", required=True)
    parser.add_argument("--source-engine-sha", required=True)
    parser.add_argument("--runtime-runner-sha", required=True)
    parser.add_argument("--runtime-engine-sha", required=True)
    args = parser.parse_args()
    execute_audio_resume(
        bundle_dir=args.bundle,
        durable_history=args.durable_history,
        accepted_history_output=args.accepted_history_output,
        result_output=args.result,
        expected_source_run_id=args.source_run_id,
        expected_source_runner_sha=args.source_runner_sha,
        expected_source_engine_sha=args.source_engine_sha,
        expected_runtime_runner_sha=args.runtime_runner_sha,
        expected_runtime_engine_sha=args.runtime_engine_sha,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

"""Capture only typed, recoverable Audio Production V2 audit unavailability.

A confirmed semantic mismatch is a real quality block and must never become resumable.
Only ``AudioProductionContractError(AUDIT_UNAVAILABLE)`` is eligible, after proving the
same final.mp4 bytes already passed Producer/retention preflight and after persisting the
in-memory Audio Semantic Integrity provenance needed by a later process.
"""

from copy import deepcopy
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from isco_video_agent.anti_repetition import load_history
from isco_video_agent.brief_approval_binding import verify_brief_approval
from isco_video_agent.production_pipeline import _output_key

from scripts.audio_producer_final_certificate import require_audio_producer_certificate
from scripts.audio_production_contract_v2 import (
    AUDIT_FILENAME,
    AudioContractErrorCode,
    AudioProductionContractError,
)
from scripts.audio_retention_qc import REPORT_FILENAME as RETENTION_REPORT_FILENAME
from scripts.audio_semantic_resume_state_v1 import (
    FILENAME as SEMANTIC_RESUME_FILENAME,
    export_audio_semantic_resume_state,
)


CONTRACT_ID = "audio.qc-pending.v1"
SCHEMA_VERSION = 1
FILENAME = "audio-qc-pending.json"
STATUS = "AUDIO_QC_PENDING_PROVIDER_AUDIT"
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA64 = re.compile(r"^[0-9a-f]{64}$")


class AudioQCPendingCheckpointError(RuntimeError):
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
        raise AudioQCPendingCheckpointError(f"audio_qc_pending_invalid_json:{Path(path).name}") from exc
    if not isinstance(value, dict):
        raise AudioQCPendingCheckpointError(f"audio_qc_pending_wrong_shape:{Path(path).name}")
    return value


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    except Exception:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise


def is_audio_qc_pending_error(exc: BaseException) -> bool:
    return (
        isinstance(exc, AudioProductionContractError)
        and exc.code is AudioContractErrorCode.AUDIT_UNAVAILABLE
    )


def _source_identity() -> dict[str, str]:
    runner_sha = str(os.environ.get("GITHUB_SHA") or "").strip().lower()
    engine_sha = str(os.environ.get("ISCO_ENGINE_SHA") or "").strip().lower()
    run_id = str(os.environ.get("GITHUB_RUN_ID") or "").strip()
    run_attempt = str(os.environ.get("GITHUB_RUN_ATTEMPT") or "").strip()
    run_number = str(os.environ.get("GITHUB_RUN_NUMBER") or "").strip()
    production_id = str(os.environ.get("ISCO_PRODUCTION_ID") or "").strip()
    if not _SHA40.fullmatch(runner_sha):
        raise AudioQCPendingCheckpointError("audio_qc_pending_runner_sha_invalid")
    if not _SHA40.fullmatch(engine_sha):
        raise AudioQCPendingCheckpointError("audio_qc_pending_engine_sha_invalid")
    if not run_id.isdigit() or not run_attempt.isdigit() or not run_number.isdigit():
        raise AudioQCPendingCheckpointError("audio_qc_pending_run_identity_invalid")
    expected_production_id = f"v4:{run_id}:{run_attempt}"
    if production_id != expected_production_id:
        raise AudioQCPendingCheckpointError("audio_qc_pending_production_id_mismatch")
    return {
        "run_id": run_id,
        "run_attempt": run_attempt,
        "run_number": run_number,
        "runner_sha": runner_sha,
        "engine_sha": engine_sha,
        "production_id": production_id,
    }


def _pending_production_record(output_key: str) -> dict[str, Any]:
    try:
        data = load_history()
    except Exception as exc:
        raise AudioQCPendingCheckpointError("audio_qc_pending_history_unreadable") from exc
    videos = data.get("videos") if isinstance(data, dict) else None
    if not isinstance(videos, list):
        raise AudioQCPendingCheckpointError("audio_qc_pending_history_videos_invalid")
    matches = [
        item
        for item in videos
        if isinstance(item, dict) and str(item.get("output") or "").strip() == output_key
    ]
    if len(matches) != 1:
        raise AudioQCPendingCheckpointError("audio_qc_pending_requires_exactly_one_history_row")
    record = matches[0]
    if str(record.get("release_status") or "").strip() == "accepted_after_final_critic":
        raise AudioQCPendingCheckpointError("audio_qc_pending_refused_already_accepted_history_row")
    return deepcopy(record)


def _require_retention_binding(root: Path, final_sha: str) -> dict[str, Any]:
    report = _read_object(root / RETENTION_REPORT_FILENAME)
    if report.get("status") != "pass":
        raise AudioQCPendingCheckpointError("audio_qc_pending_retention_not_pass")
    final = report.get("final")
    if not isinstance(final, dict) or str(final.get("sha256") or "").strip().lower() != final_sha:
        raise AudioQCPendingCheckpointError("audio_qc_pending_retention_final_identity_mismatch")
    if list(report.get("blocking_findings") or []):
        raise AudioQCPendingCheckpointError("audio_qc_pending_retention_has_blocking_findings")
    return report


def _approved_control_request_snapshot(control_path: str) -> dict[str, Any] | None:
    if not control_path:
        return None
    path = Path(control_path)
    if not path.is_file():
        raise AudioQCPendingCheckpointError("audio_qc_pending_control_request_missing")
    request = _read_object(path)
    if request.get("approved_by_user") is not True:
        raise AudioQCPendingCheckpointError("audio_qc_pending_control_request_not_user_approved")
    request_id = str(request.get("request_id") or "").strip()
    request_sha = str(request.get("request_sha256") or "").strip().lower()
    if not request_id or not _SHA64.fullmatch(request_sha):
        raise AudioQCPendingCheckpointError("audio_qc_pending_control_request_identity_invalid")
    return deepcopy(request)


def _approved_brief_snapshot() -> tuple[dict[str, Any], str]:
    raw_path = str(os.environ.get("ISCO_APPROVED_BRIEF_PATH") or "").strip()
    approved_sha = str(os.environ.get("ISCO_APPROVED_BRIEF_SHA256") or "").strip().lower()
    if not raw_path or not _SHA64.fullmatch(approved_sha):
        raise AudioQCPendingCheckpointError("audio_qc_pending_manual_long_approved_brief_binding_missing")
    path = Path(raw_path)
    if not path.is_file():
        raise AudioQCPendingCheckpointError("audio_qc_pending_manual_long_approved_brief_missing")
    brief = _read_object(path)
    if brief.get("approved_by_user") is not True:
        raise AudioQCPendingCheckpointError("audio_qc_pending_manual_long_brief_not_user_approved")
    try:
        verified = str(verify_brief_approval(brief, approved_sha)).strip().lower()
    except Exception as exc:
        raise AudioQCPendingCheckpointError("audio_qc_pending_manual_long_brief_verification_failed") from exc
    if verified != approved_sha:
        raise AudioQCPendingCheckpointError("audio_qc_pending_manual_long_brief_hash_mismatch")
    return deepcopy(brief), approved_sha


def _attach_diagnostics_summary(root: Path, checkpoint: dict[str, Any]) -> None:
    path = root / "production-failure-diagnostics.json"
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return
        data["audio_qc_pending"] = {
            "contract_id": checkpoint["contract_id"],
            "status": checkpoint["status"],
            "resumable": True,
            "final_sha256": checkpoint["final"]["sha256"],
            "resume_scope": checkpoint["retry_policy"]["resume_scope"],
        }
        _atomic_json(path, data)
    except Exception:
        return


def capture_audio_qc_pending_checkpoint(
    output_dir: Path,
    exc: BaseException,
) -> Path | None:
    """Capture exact post-render state only for typed Audio V2 audit unavailability."""
    if not is_audio_qc_pending_error(exc):
        return None

    root = Path(output_dir).resolve()
    final_path = root / "final.mp4"
    if not final_path.is_file() or final_path.stat().st_size <= 1024:
        raise AudioQCPendingCheckpointError("audio_qc_pending_final_missing")
    final_sha = _sha256_file(final_path)

    audit = _read_object(root / AUDIT_FILENAME)
    if audit.get("decision") != "block" or audit.get("error_code") != AudioContractErrorCode.AUDIT_UNAVAILABLE.value:
        raise AudioQCPendingCheckpointError("audio_qc_pending_audio_audit_not_typed_unavailable")
    if str(audit.get("final_sha256") or "").strip().lower() != final_sha:
        raise AudioQCPendingCheckpointError("audio_qc_pending_audio_audit_final_identity_mismatch")
    attempts = [item for item in list(audit.get("attempts") or []) if isinstance(item, dict)]
    if not attempts or len(attempts) > 2:
        raise AudioQCPendingCheckpointError("audio_qc_pending_audio_attempt_evidence_invalid")
    if not any(item.get("status") == "technical_failure" for item in attempts):
        raise AudioQCPendingCheckpointError("audio_qc_pending_requires_provider_technical_failure")

    receipt = require_audio_producer_certificate(root)
    if str(receipt.get("final_sha256") or "").strip().lower() != final_sha:
        raise AudioQCPendingCheckpointError("audio_qc_pending_producer_receipt_identity_mismatch")
    retention = _require_retention_binding(root, final_sha)
    semantic_state = export_audio_semantic_resume_state(root)
    if str((semantic_state.get("final") or {}).get("sha256") or "").strip().lower() != final_sha:
        raise AudioQCPendingCheckpointError("audio_qc_pending_semantic_state_identity_mismatch")

    plan = _read_object(root / "plan.json")
    fmt = str(plan.get("format") or "").strip().lower()
    if fmt not in {"film", "story", "moment"}:
        raise AudioQCPendingCheckpointError("audio_qc_pending_format_invalid")
    source = _source_identity()
    output_key = _output_key(root)
    production_record = _pending_production_record(output_key)

    release_tag = str(os.environ.get("ISCO_RELEASE_TAG_OVERRIDE") or "").strip()
    if not release_tag:
        release_tag = f"video-{source['run_number']}"
    control_path = str(os.environ.get("ISCO_CONTROL_REQUEST_PATH") or "").strip()
    control_request = _approved_control_request_snapshot(control_path)
    ingress = "telegram" if control_request is not None else "manual"
    approved_brief: dict[str, Any] | None = None
    approved_brief_sha256: str | None = None
    if ingress == "manual" and fmt in {"film", "story"}:
        approved_brief, approved_brief_sha256 = _approved_brief_snapshot()

    checkpoint = {
        "schema_version": SCHEMA_VERSION,
        "contract_id": CONTRACT_ID,
        "status": STATUS,
        "release_allowed": False,
        "resumable": True,
        "format": fmt,
        "ingress": ingress,
        "release_tag": release_tag,
        "control_request": control_request,
        "approved_brief": approved_brief,
        "approved_brief_sha256": approved_brief_sha256,
        "source": source,
        "final": {
            "path": "final.mp4",
            "sha256": final_sha,
            "bytes": final_path.stat().st_size,
        },
        "audio_audit": {
            "contract_id": audit.get("contract_id"),
            "error_code": audit.get("error_code"),
            "attempts": attempts,
            "sha256": _sha256_file(root / AUDIT_FILENAME),
        },
        "producer_receipt": {
            "phase": receipt.get("phase"),
            "decision": receipt.get("decision"),
            "final_sha256": receipt.get("final_sha256"),
        },
        "retention": {
            "status": retention.get("status"),
            "scope": retention.get("scope"),
            "final_sha256": final_sha,
            "sha256": _sha256_file(root / RETENTION_REPORT_FILENAME),
        },
        "semantic_resume_state": {
            "file": SEMANTIC_RESUME_FILENAME,
            "contract_id": semantic_state.get("contract_id"),
            "sha256": _sha256_file(root / SEMANTIC_RESUME_FILENAME),
        },
        "production_state": {
            "output_key": output_key,
            "record": production_record,
            "accepted": False,
        },
        "retry_policy": {
            "resume_scope": "audio_semantic_audit_then_existing_final_master_gold_chain",
            "replanning_allowed": False,
            "research_allowed": False,
            "visual_retrieval_allowed": False,
            "tts_allowed": False,
            "parent_rerender_allowed": False,
            "audio_audit_revalidation_required": True,
            "final_master_qc_revalidation_required": True,
            "gold_revalidation_required": True,
            "semantic_mismatch_is_resumable": False,
            "provider_attempts_per_audio_revalidation_max": 2,
            "resume_execution_limit": 1,
        },
    }
    target = root / FILENAME
    _atomic_json(target, checkpoint)
    _attach_diagnostics_summary(root, checkpoint)
    print(
        "AUDIO_QC_PENDING captured: "
        f"format={fmt} final_sha256={final_sha[:12]} source_run={source['run_id']}"
    )
    return target

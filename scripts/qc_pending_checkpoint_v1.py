from __future__ import annotations

"""Fail-closed post-render checkpoint for temporary Gold Vision provider exhaustion.

The checkpoint never authorizes release. It records that the exact rendered bytes had
already passed Final Master Acceptance before the Gold provider mesh became unavailable.
Only the explicit VisionProviderMeshUnavailableError taxonomy is eligible; similarly
worded runtime errors are never promoted to a resumable state.

V2 additionally binds the exact pre-Gold production-history row. Gold correctly removes
that row from durable success memory on failure; a later Gold-only resume can therefore
inject the bound row into a temporary history copy and persist it only after Gold succeeds.
Failed resume attempts never contaminate durable accepted-memory state.
"""

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from scripts.final_master_acceptance_v2 import require_final_master_acceptance
from scripts import vision_stage_contract_v2 as vision_contract


CONTRACT_ID = "gold.qc-pending.v1"
CONTRACT_VERSION = 2
FILENAME = "qc-pending.json"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _is_gold_vision_mesh_exhaustion(exc: BaseException) -> bool:
    return isinstance(exc, vision_contract.legacy.VisionProviderMeshUnavailableError)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _validate_pending_production_record(record: object, *, output_key: str) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise RuntimeError("QC_PENDING refused: pre-Gold production record is missing")
    normalized_output = str(record.get("output") or "").strip()
    if not output_key or normalized_output != output_key:
        raise RuntimeError("QC_PENDING refused: pre-Gold production record output mismatch")
    if str(record.get("release_status") or "").strip() == "accepted_after_final_critic":
        raise RuntimeError("QC_PENDING refused: pre-Gold production record was already accepted")
    # JSON round-trip gives the checkpoint an immutable, serialization-safe copy instead
    # of retaining a mutable reference to Engine learning state.
    try:
        copied = json.loads(json.dumps(record, ensure_ascii=False, sort_keys=True, default=str))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("QC_PENDING refused: pre-Gold production record is not serializable") from exc
    if not isinstance(copied, dict):
        raise RuntimeError("QC_PENDING refused: pre-Gold production record is malformed")
    return copied


def capture_qc_pending_checkpoint(
    output_dir: Path,
    exc: BaseException,
    *,
    production_record: dict[str, Any] | None = None,
    output_key: str | None = None,
) -> dict[str, Any] | None:
    """Write a resumable marker only for exact provider-mesh exhaustion after Final QC PASS."""
    if not _is_gold_vision_mesh_exhaustion(exc):
        return None
    root = Path(output_dir)
    # The Runner's outer failure handler may call us after Gold already captured the
    # authoritative checkpoint with the pre-cleanup history row. Revalidate rather than
    # overwriting it with a record that is no longer available after fail-closed cleanup.
    if production_record is None and (root / FILENAME).is_file():
        return verify_qc_pending_checkpoint(root)

    final_path = root / "final.mp4"
    qc_path = root / "final-master-qc.json"
    if not final_path.is_file() or not qc_path.is_file():
        return None

    acceptance = require_final_master_acceptance(root)
    acceptance_contract = acceptance.get("acceptance_contract") or {}
    sources = acceptance_contract.get("sources") or {}
    certified_final = (sources.get("final") or {}).get("sha256")
    final_sha = _sha256_file(final_path)
    if str(certified_final or "").strip().lower() != final_sha:
        raise RuntimeError("QC_PENDING refused: Final Master Acceptance does not bind current final.mp4")

    gold_report_path = root / "gold-enforce-report.json"
    gold_report = _read_json(gold_report_path)
    if gold_report is not None:
        same_render = gold_report.get("same_render") or {}
        if same_render.get("artifact_divergence") is True:
            raise RuntimeError("QC_PENDING refused: Gold observed final.mp4 divergence")
        gold = gold_report.get("gold") or {}
        if gold.get("accepted") is True:
            raise RuntimeError("QC_PENDING refused: Gold already accepted this render")

    normalized_output_key = str(output_key or "").strip()
    pending_record = _validate_pending_production_record(
        production_record,
        output_key=normalized_output_key,
    )
    record_sha = _canonical_sha256(pending_record)

    document: dict[str, Any] = {
        "schema_version": CONTRACT_VERSION,
        "contract_id": CONTRACT_ID,
        "status": "GOLD_VISION_PENDING_PROVIDER_CAPACITY",
        "release_allowed": False,
        "resumable": True,
        "resume_authority": "gold_only_after_exact_revalidation",
        "resume_scope": "gold_enforcement_only_no_replan_no_retrieval_no_rerender_no_tts",
        "format": str((_read_json(root / "plan.json") or {}).get("format") or "").strip() or None,
        "final": {
            "file": "final.mp4",
            "sha256": final_sha,
            "byte_length": final_path.stat().st_size,
        },
        "final_master_acceptance": {
            "contract_id": acceptance_contract.get("contract_id"),
            "decision": acceptance_contract.get("decision"),
            "sha256": _sha256_file(qc_path),
        },
        "gold_enforce_report_sha256": (
            _sha256_file(gold_report_path) if gold_report_path.is_file() else None
        ),
        "production_state": {
            "output_key": normalized_output_key,
            "record_sha256": record_sha,
            "record": pending_record,
            "durable_history_policy": "inject_into_temporary_history_and_persist_only_after_gold_pass",
        },
        "runner_sha": str(os.environ.get("GITHUB_SHA") or "").strip() or None,
        "engine_sha": str(os.environ.get("ISCO_ENGINE_SHA") or "").strip() or None,
        "source_run_id": str(os.environ.get("GITHUB_RUN_ID") or "").strip() or None,
        "source_run_attempt": str(os.environ.get("GITHUB_RUN_ATTEMPT") or "").strip() or None,
        "git_ref": str(os.environ.get("GITHUB_REF") or "").strip() or None,
        "failure_type": type(exc).__name__,
        "failure_taxonomy": "VisionProviderMeshUnavailableError",
        "retry_policy": {
            "rerender_required": False,
            "gold_revalidation_required": True,
            "master_or_delivery_allowed_before_gold": False,
            "replanning_allowed": False,
            "visual_retrieval_allowed": False,
            "tts_allowed": False,
        },
    }
    path = root / FILENAME
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")

    diagnostics_path = root / "production-failure-diagnostics.json"
    diagnostics = _read_json(diagnostics_path)
    if diagnostics is not None:
        diagnostics["qc_pending_checkpoint"] = {
            key: value for key, value in document.items() if key != "production_state"
        }
        diagnostics["qc_pending_checkpoint"]["production_state"] = {
            "output_key": normalized_output_key,
            "record_sha256": record_sha,
            "durable_history_policy": document["production_state"]["durable_history_policy"],
        }
        diagnostics_path.write_text(
            json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(
        "QC_PENDING V2 captured: exact Final Master PASS + core state retained; Gold release remains blocked; "
        f"final_sha256={final_sha[:12]} record_sha256={record_sha[:12]}"
    )
    return document


def verify_qc_pending_checkpoint(output_dir: Path) -> dict[str, Any]:
    root = Path(output_dir)
    document = _read_json(root / FILENAME)
    if document is None or document.get("contract_id") != CONTRACT_ID:
        raise RuntimeError("QC_PENDING checkpoint missing or invalid")
    if document.get("schema_version") != CONTRACT_VERSION:
        raise RuntimeError("QC_PENDING checkpoint schema is not resumable by this runtime")
    if document.get("release_allowed") is not False or document.get("resumable") is not True:
        raise RuntimeError("QC_PENDING checkpoint cannot authorize release")
    if document.get("failure_taxonomy") != "VisionProviderMeshUnavailableError":
        raise RuntimeError("QC_PENDING checkpoint has unsupported failure taxonomy")
    final_path = root / str((document.get("final") or {}).get("file") or "final.mp4")
    if not final_path.is_file():
        raise RuntimeError("QC_PENDING final render missing")
    expected_final_sha = str((document.get("final") or {}).get("sha256") or "")
    if _sha256_file(final_path) != expected_final_sha:
        raise RuntimeError("QC_PENDING final render hash mismatch")
    if final_path.stat().st_size != int((document.get("final") or {}).get("byte_length") or -1):
        raise RuntimeError("QC_PENDING final render byte length mismatch")

    acceptance = require_final_master_acceptance(root)
    acceptance_contract = acceptance.get("acceptance_contract") or {}
    certified_final = ((acceptance_contract.get("sources") or {}).get("final") or {}).get("sha256")
    if str(certified_final or "").strip().lower() != expected_final_sha:
        raise RuntimeError("QC_PENDING Final Master Acceptance no longer binds final.mp4")

    state = document.get("production_state") or {}
    output_key = str(state.get("output_key") or "").strip()
    pending_record = _validate_pending_production_record(state.get("record"), output_key=output_key)
    if _canonical_sha256(pending_record) != str(state.get("record_sha256") or ""):
        raise RuntimeError("QC_PENDING production record hash mismatch")
    return document
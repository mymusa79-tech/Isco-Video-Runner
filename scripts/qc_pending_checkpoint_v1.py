from __future__ import annotations

"""Fail-closed post-render checkpoint for temporary Gold Vision provider exhaustion.

The checkpoint never authorizes release. It records that the exact rendered bytes had
already passed Final Master Acceptance before the Gold provider mesh became unavailable.
Only the explicit VisionProviderMeshUnavailableError taxonomy is eligible; similarly
worded runtime errors are never promoted to a resumable state.
"""

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from scripts.final_master_acceptance_v2 import require_final_master_acceptance
from scripts import vision_stage_contract_v2 as vision_contract


CONTRACT_ID = "gold.qc-pending.v1"
CONTRACT_VERSION = 1
FILENAME = "qc-pending.json"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_gold_vision_mesh_exhaustion(exc: BaseException) -> bool:
    return isinstance(exc, vision_contract.legacy.VisionProviderMeshUnavailableError)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def capture_qc_pending_checkpoint(output_dir: Path, exc: BaseException) -> dict[str, Any] | None:
    """Write a pending marker only for exact provider-mesh exhaustion after Final QC PASS."""
    if not _is_gold_vision_mesh_exhaustion(exc):
        return None
    root = Path(output_dir)
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

    document: dict[str, Any] = {
        "schema_version": CONTRACT_VERSION,
        "contract_id": CONTRACT_ID,
        "status": "GOLD_VISION_PENDING_PROVIDER_CAPACITY",
        "release_allowed": False,
        "resume_authority": "gold_only_after_exact_revalidation",
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
        "runner_sha": str(os.environ.get("GITHUB_SHA") or "").strip() or None,
        "engine_sha": str(os.environ.get("ISCO_ENGINE_SHA") or "").strip() or None,
        "failure_type": type(exc).__name__,
        "failure_taxonomy": "VisionProviderMeshUnavailableError",
        "retry_policy": {
            "rerender_required": False,
            "gold_revalidation_required": True,
            "master_or_delivery_allowed_before_gold": False,
        },
    }
    path = root / FILENAME
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")

    diagnostics_path = root / "production-failure-diagnostics.json"
    diagnostics = _read_json(diagnostics_path)
    if diagnostics is not None:
        diagnostics["qc_pending_checkpoint"] = document
        diagnostics_path.write_text(
            json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(
        "QC_PENDING V1 captured: exact Final Master PASS retained; Gold release remains blocked; "
        f"final_sha256={final_sha[:12]}"
    )
    return document


def verify_qc_pending_checkpoint(output_dir: Path) -> dict[str, Any]:
    root = Path(output_dir)
    document = _read_json(root / FILENAME)
    if document is None or document.get("contract_id") != CONTRACT_ID:
        raise RuntimeError("QC_PENDING checkpoint missing or invalid")
    if document.get("release_allowed") is not False:
        raise RuntimeError("QC_PENDING checkpoint cannot authorize release")
    if document.get("failure_taxonomy") != "VisionProviderMeshUnavailableError":
        raise RuntimeError("QC_PENDING checkpoint has unsupported failure taxonomy")
    final_path = root / str((document.get("final") or {}).get("file") or "final.mp4")
    if not final_path.is_file():
        raise RuntimeError("QC_PENDING final render missing")
    if _sha256_file(final_path) != str((document.get("final") or {}).get("sha256") or ""):
        raise RuntimeError("QC_PENDING final render hash mismatch")
    require_final_master_acceptance(root)
    return document

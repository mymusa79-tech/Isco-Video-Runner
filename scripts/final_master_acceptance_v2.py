from __future__ import annotations

"""Format-aware F24 compatibility seam.

The original F24 implementation is preserved byte-for-byte in
``final_master_acceptance_v2_legacy.py``. This shim changes only source ownership:
long-form receipts still bind the M7 visual timeline, while Moment receipts bind
the measured Short artifacts they actually own. The upload-conformance policy,
exact final-byte validation, and fail-closed semantics remain in the legacy F24
implementation.
"""

import hashlib
import json
from pathlib import Path
from typing import Any

from scripts import final_master_acceptance_v2_legacy as _legacy
from scripts.final_master_format_router import (
    ROUTER_ID,
    ROUTER_VERSION,
    implementation_sha256 as router_implementation_sha256,
    install_format_aware_qc_router,
)


CONTRACT_ID = _legacy.CONTRACT_ID
CONTRACT_SCHEMA_VERSION = _legacy.CONTRACT_SCHEMA_VERSION
REPORT_FILENAME = _legacy.REPORT_FILENAME
FinalMasterAcceptanceError = _legacy.FinalMasterAcceptanceError


def _read_format(root: Path) -> str:
    plan = _legacy._read_object(Path(root) / "plan.json")
    quality = _legacy._read_object(Path(root) / "quality-final.json")
    fmt = str(plan.get("format") or quality.get("format") or "").strip().lower()
    if fmt not in {"film", "story", "moment"}:
        raise FinalMasterAcceptanceError(
            "final_master_acceptance_unsupported_format"
        )
    return fmt


def _source_bindings(root: Path) -> dict[str, dict[str, Any]]:
    root = Path(root)
    fmt = _read_format(root)
    result = {
        "final": _legacy._regular_file_binding(root / "final.mp4"),
        "plan": _legacy._regular_file_binding(root / "plan.json"),
        "quality_final": _legacy._regular_file_binding(root / "quality-final.json"),
    }
    if fmt in {"film", "story"}:
        result["visual_timeline"] = _legacy._regular_file_binding(
            root / "visual-timeline.json"
        )
        return result

    short_timeline = root / "short-visual-timeline.json"
    if short_timeline.exists() or short_timeline.is_symlink():
        result["short_visual_timeline"] = _legacy._regular_file_binding(
            short_timeline
        )
    return result


def qc_policy_fingerprint() -> str:
    policy = {
        "contract_id": CONTRACT_ID,
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "certified_qc_implementation_sha256": _legacy._implementation_sha256(),
        "legacy_acceptance_implementation_sha256": hashlib.sha256(
            Path(_legacy.__file__).read_bytes()
        ).hexdigest(),
        "format_router": {
            "id": ROUTER_ID,
            "version": ROUTER_VERSION,
            "implementation_sha256": router_implementation_sha256(),
        },
        "source_ownership": {
            "film_story": [
                "final.mp4",
                "plan.json",
                "quality-final.json",
                "visual-timeline.json",
            ],
            "moment_base_or_sibling": [
                "final.mp4",
                "plan.json",
                "quality-final.json",
            ],
            "moment_cinematic_optional": "short-visual-timeline.json",
            "long_m7_timeline_mandatory": True,
            "moment_m7_timeline_forbidden_as_authority": True,
        },
        "upload_conformance": {
            "h264_profile": "High when reported",
            "field_order": "progressive or unknown when reported",
            "aac_profile": "LC/AAC LC when reported",
            "sdr_color_tags": "BT.709 when all tags reported",
            "explicit_hdr": "block",
            "missing_optional_metadata": "warn",
            "mp4_fast_start": "warn_only",
        },
        "media_mutation": "forbidden",
        "ai_calls_added": 0,
    }
    payload = json.dumps(
        policy,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# Redirect the preserved F24 implementation to the format-aware ownership seam.
# Its functions resolve these globals at call time, so upload checks and exact-byte
# validation stay entirely in the preserved implementation.
_legacy._source_bindings = _source_bindings
_legacy.qc_policy_fingerprint = qc_policy_fingerprint

seal_final_master_acceptance = _legacy.seal_final_master_acceptance
require_final_master_acceptance = _legacy.require_final_master_acceptance
require_certified_final_video = _legacy.require_certified_final_video
final_master_acceptance_sha256 = _legacy.final_master_acceptance_sha256


def __getattr__(name: str):
    return getattr(_legacy, name)


# Imported by final_qc_observer_durability during runtime_closure import.
# The wrapper is inert outside canonical runtime, preserving direct core unit tests.
install_format_aware_qc_router()

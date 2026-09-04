from __future__ import annotations

"""Format-aware F24 compatibility seam.

The original F24 implementation is preserved byte-for-byte in
``final_master_acceptance_v2_legacy.py``. This shim changes only source ownership:
long-form receipts still bind the M7 visual timeline, while Moment receipts bind
the measured Short artifacts they actually own. The upload-conformance policy,
exact final-byte validation, and fail-closed semantics remain in the legacy F24
implementation.

The format router is deliberately *not* imported here. F24 is reachable from the
durable planning source closure, while Final Master QC is post-planning media policy.
The router is installed later from the runtime media preflight so this acceptance
shim cannot pull media/QC code into the planning checkpoint contract.
"""

import hashlib
import json
from pathlib import Path
from typing import Any

from scripts import final_master_acceptance_v2_legacy as _legacy


CONTRACT_ID = _legacy.CONTRACT_ID
CONTRACT_SCHEMA_VERSION = _legacy.CONTRACT_SCHEMA_VERSION
REPORT_FILENAME = _legacy.REPORT_FILENAME
FinalMasterAcceptanceError = _legacy.FinalMasterAcceptanceError
ROUTER_ID = "final-master-format-router-v1"
ROUTER_VERSION = 1
_ROUTER_PATH = Path(__file__).with_name("final_master_format_router.py")

# Compatibility surfaces retained for existing tests/diagnostics that monkeypatch the
# public F24 module. Production does not replace these callables.
probe = _legacy.probe
_mp4_fast_start = _legacy._mp4_fast_start


def _router_implementation_sha256() -> str:
    try:
        if _ROUTER_PATH.is_symlink() or not _ROUTER_PATH.is_file():
            raise OSError("router is not a regular file")
        return hashlib.sha256(_ROUTER_PATH.read_bytes()).hexdigest()
    except OSError as exc:
        raise FinalMasterAcceptanceError(
            "final_master_acceptance_missing_format_router"
        ) from exc


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
            "implementation_sha256": _router_implementation_sha256(),
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


def _with_public_upload_overrides(callable_, *args, **kwargs):
    old_probe = _legacy.probe
    old_fast_start = _legacy._mp4_fast_start
    _legacy.probe = probe
    _legacy._mp4_fast_start = _mp4_fast_start
    try:
        return callable_(*args, **kwargs)
    finally:
        _legacy.probe = old_probe
        _legacy._mp4_fast_start = old_fast_start


def _upload_conformance(final_path: Path) -> dict[str, Any]:
    return _with_public_upload_overrides(_legacy._upload_conformance, final_path)


def seal_final_master_acceptance(output_dir: Path, report: dict[str, Any]) -> dict[str, Any]:
    return _with_public_upload_overrides(
        _legacy.seal_final_master_acceptance,
        output_dir,
        report,
    )


require_final_master_acceptance = _legacy.require_final_master_acceptance
require_certified_final_video = _legacy.require_certified_final_video
final_master_acceptance_sha256 = _legacy.final_master_acceptance_sha256


def __getattr__(name: str):
    return getattr(_legacy, name)

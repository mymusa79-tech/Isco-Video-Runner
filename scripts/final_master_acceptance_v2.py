from __future__ import annotations

import hashlib
import json
import os
import struct
import tempfile
from pathlib import Path
from typing import Any

from isco_video_agent.media.ffmpeg import probe


CONTRACT_ID = "final.master.acceptance.v2"
CONTRACT_SCHEMA_VERSION = 2
REPORT_FILENAME = "final-master-qc.json"
_CORE_PATH = Path(__file__).with_name("final_master_qc.py")
_HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}
_HDR_PRIMARIES = {"bt2020"}
_HDR_SPACES = {"bt2020nc", "bt2020c"}


class FinalMasterAcceptanceError(RuntimeError):
    pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_file_binding(path: Path) -> dict[str, Any]:
    path = Path(path)
    try:
        if path.is_symlink() or not path.is_file():
            raise FinalMasterAcceptanceError(f"final_master_acceptance_invalid_file:{path.name}")
        size = int(path.stat().st_size)
    except OSError as exc:
        raise FinalMasterAcceptanceError(f"final_master_acceptance_unreadable_file:{path.name}") from exc
    if size <= 0:
        raise FinalMasterAcceptanceError(f"final_master_acceptance_empty_file:{path.name}")
    return {"file": path.name, "sha256": _sha256_file(path), "byte_length": size}


def _read_object(path: Path) -> dict[str, Any]:
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError("not a regular file")
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise FinalMasterAcceptanceError(f"final_master_acceptance_invalid_json:{path.name}") from exc
    if not isinstance(value, dict):
        raise FinalMasterAcceptanceError(f"final_master_acceptance_wrong_shape:{path.name}")
    return value


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _source_bindings(root: Path) -> dict[str, dict[str, Any]]:
    return {
        "final": _regular_file_binding(root / "final.mp4"),
        "plan": _regular_file_binding(root / "plan.json"),
        "quality_final": _regular_file_binding(root / "quality-final.json"),
        "visual_timeline": _regular_file_binding(root / "visual-timeline.json"),
    }


def _optional_evidence_bindings(root: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for key, filename in (
        ("audio_production_contract", "audio-production-contract-v2.json"),
        ("audio_producer_repair", "audio-producer-repair.json"),
    ):
        path = root / filename
        if path.is_file() and not path.is_symlink():
            result[key] = _regular_file_binding(path)
    return result


def _implementation_sha256() -> str:
    if not _CORE_PATH.is_file():
        raise FinalMasterAcceptanceError("final_master_acceptance_missing_implementation")
    return _sha256_file(_CORE_PATH)


def qc_policy_fingerprint() -> str:
    """Fingerprint the immutable QC core plus the F24 acceptance-only policy."""
    policy = {
        "contract_id": CONTRACT_ID,
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "certified_qc_implementation_sha256": _implementation_sha256(),
        "exact_sources": ["final.mp4", "plan.json", "quality-final.json", "visual-timeline.json"],
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
    payload = json.dumps(policy, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _mp4_fast_start(final_path: Path) -> tuple[bool | None, list[str]]:
    positions: dict[str, int] = {}
    order: list[str] = []
    try:
        size = final_path.stat().st_size
        offset = 0
        with final_path.open("rb") as handle:
            while offset + 8 <= size and len(order) < 64:
                handle.seek(offset)
                header = handle.read(8)
                if len(header) != 8:
                    break
                box_size, box_type = struct.unpack(">I4s", header)
                header_size = 8
                if box_size == 1:
                    extended = handle.read(8)
                    if len(extended) != 8:
                        break
                    box_size = struct.unpack(">Q", extended)[0]
                    header_size = 16
                elif box_size == 0:
                    box_size = size - offset
                if box_size < header_size or offset + box_size > size:
                    break
                name = box_type.decode("ascii", errors="replace")
                order.append(name)
                positions.setdefault(name, offset)
                if "moov" in positions and "mdat" in positions:
                    return positions["moov"] < positions["mdat"], order
                offset += int(box_size)
    except OSError:
        return None, order
    if "moov" not in positions or "mdat" not in positions:
        return None, order
    return positions["moov"] < positions["mdat"], order


def _upload_conformance(final_path: Path) -> dict[str, Any]:
    try:
        info = probe(final_path)
    except Exception as exc:
        raise FinalMasterAcceptanceError(
            f"final_master_acceptance_probe_failed:{type(exc).__name__}"
        ) from exc
    streams = info.get("streams") if isinstance(info, dict) else None
    if not isinstance(streams, list):
        streams = []
    videos = [item for item in streams if isinstance(item, dict) and item.get("codec_type") == "video"]
    audios = [item for item in streams if isinstance(item, dict) and item.get("codec_type") == "audio"]
    if len(videos) != 1:
        raise FinalMasterAcceptanceError("final_master_acceptance_video_stream_identity_invalid")

    video = videos[0]
    warnings: list[str] = []
    blocking: list[str] = []
    profile = str(video.get("profile") or "").strip()
    field_order = str(video.get("field_order") or "").strip().casefold()
    transfer = str(video.get("color_transfer") or "").strip().casefold()
    primaries = str(video.get("color_primaries") or "").strip().casefold()
    space = str(video.get("color_space") or "").strip().casefold()

    if profile and profile.casefold() != "high":
        blocking.append(f"unexpected_h264_profile={profile}")
    elif not profile:
        warnings.append("h264_profile_missing")

    if field_order and field_order not in {"progressive", "unknown"}:
        blocking.append(f"unexpected_field_order={field_order}")
    elif not field_order:
        warnings.append("field_order_missing")

    explicit_hdr = transfer in _HDR_TRANSFERS or primaries in _HDR_PRIMARIES or space in _HDR_SPACES
    if explicit_hdr:
        blocking.append(
            "unexpected_hdr_master="
            f"transfer:{transfer or 'missing'},primaries:{primaries or 'missing'},space:{space or 'missing'}"
        )
    if not transfer or not primaries or not space:
        warnings.append("color_tags_incomplete_but_no_explicit_hdr_tag")
    elif not explicit_hdr and {transfer, primaries, space} != {"bt709"}:
        blocking.append(
            "unexpected_sdr_color_tags="
            f"transfer:{transfer},primaries:{primaries},space:{space}"
        )

    audio_profile = ""
    if audios:
        audio_profile = str(audios[0].get("profile") or "").strip()
        if audio_profile and audio_profile.casefold() not in {"lc", "aac lc"}:
            blocking.append(f"unexpected_aac_profile={audio_profile}")
        elif not audio_profile:
            warnings.append("aac_profile_missing")

    fast_start, boxes = _mp4_fast_start(final_path)
    if fast_start is False:
        warnings.append("mp4_fast_start_not_present")
    elif fast_start is None:
        warnings.append("mp4_fast_start_not_observable")

    return {
        "decision": "block" if blocking else "pass",
        "blocking_findings": blocking,
        "warnings": warnings,
        "observed": {
            "h264_profile": profile or None,
            "field_order": field_order or None,
            "color_transfer": transfer or None,
            "color_primaries": primaries or None,
            "color_space": space or None,
            "aac_profile": audio_profile or None,
            "mp4_fast_start": fast_start,
            "top_level_boxes_observed": boxes,
        },
    }


def _validate_contract_document(document: dict[str, Any]) -> dict[str, Any]:
    if not (
        document.get("status") == "pass"
        and document.get("production_stage") == "post_render_pre_gold_acceptance"
        and document.get("full_decode_ok") is True
        and document.get("full_decode_timed_out") is False
        and document.get("final_media_mutated") is False
        and not list(document.get("blocking_findings") or [])
    ):
        raise FinalMasterAcceptanceError("final_master_acceptance_qc_not_pass")

    upload = document.get("upload_conformance")
    if not isinstance(upload, dict) or upload.get("decision") != "pass":
        raise FinalMasterAcceptanceError("final_master_acceptance_upload_conformance_not_pass")
    if list(upload.get("blocking_findings") or []):
        raise FinalMasterAcceptanceError("final_master_acceptance_upload_conformance_blocking")

    acceptance = document.get("acceptance_contract")
    if not isinstance(acceptance, dict):
        raise FinalMasterAcceptanceError("final_master_acceptance_missing_contract")
    if acceptance.get("contract_id") != CONTRACT_ID:
        raise FinalMasterAcceptanceError("final_master_acceptance_contract_id_mismatch")
    if int(acceptance.get("schema_version") or 0) != CONTRACT_SCHEMA_VERSION:
        raise FinalMasterAcceptanceError("final_master_acceptance_schema_mismatch")
    if acceptance.get("decision") != "pass":
        raise FinalMasterAcceptanceError("final_master_acceptance_decision_mismatch")
    if acceptance.get("qc_policy_fingerprint") != qc_policy_fingerprint():
        raise FinalMasterAcceptanceError("final_master_acceptance_policy_drift")
    if acceptance.get("implementation_sha256") != _implementation_sha256():
        raise FinalMasterAcceptanceError("final_master_acceptance_implementation_drift")
    if not isinstance(acceptance.get("sources"), dict):
        raise FinalMasterAcceptanceError("final_master_acceptance_missing_sources")
    if not isinstance(acceptance.get("upstream_evidence"), dict):
        raise FinalMasterAcceptanceError("final_master_acceptance_invalid_upstream_evidence")
    return acceptance


def seal_final_master_acceptance(output_dir: Path, report: dict[str, Any]) -> dict[str, Any]:
    """Atomically upgrade a certified-core QC result into the exact-artifact P4 receipt."""
    root = Path(output_dir)
    if report.get("status") != "pass":
        raise FinalMasterAcceptanceError("final_master_acceptance_requires_core_pass")

    upload = _upload_conformance(root / "final.mp4")
    sealed = dict(report)
    sealed["schema_version"] = CONTRACT_SCHEMA_VERSION
    sealed["upload_conformance"] = upload
    sealed["warnings"] = list(report.get("warnings") or []) + list(upload.get("warnings") or [])
    sealed["blocking_findings"] = list(report.get("blocking_findings") or []) + list(
        upload.get("blocking_findings") or []
    )
    sealed["status"] = "block" if sealed["blocking_findings"] else "pass"

    acceptance = {
        "contract_id": CONTRACT_ID,
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "decision": sealed["status"],
        "production_stage": "post_render_pre_gold_acceptance",
        "qc_policy_fingerprint": qc_policy_fingerprint(),
        "implementation_sha256": _implementation_sha256(),
        "runner_sha": (os.environ.get("GITHUB_SHA") or "").strip().lower() or None,
        "engine_sha": (os.environ.get("ISCO_ENGINE_SHA") or "").strip().lower() or None,
        "sources": _source_bindings(root),
        "upstream_evidence": _optional_evidence_bindings(root),
    }
    sealed["acceptance_contract"] = acceptance
    _atomic_json(root / REPORT_FILENAME, sealed)

    if sealed["status"] != "pass":
        raise FinalMasterAcceptanceError("final_master_acceptance_upload_conformance_block")
    return sealed


def require_final_master_acceptance(
    output_dir: Path,
    *,
    report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fail closed unless the P4 PASS receipt belongs to all current exact artifacts."""
    root = Path(output_dir)
    document = dict(report) if isinstance(report, dict) else _read_object(root / REPORT_FILENAME)
    acceptance = _validate_contract_document(document)

    if acceptance["sources"] != _source_bindings(root):
        raise FinalMasterAcceptanceError("final_master_acceptance_artifact_identity_mismatch")

    for key, binding in acceptance["upstream_evidence"].items():
        if not isinstance(binding, dict):
            raise FinalMasterAcceptanceError("final_master_acceptance_invalid_upstream_binding")
        filename = str(binding.get("file") or "")
        if not filename or _regular_file_binding(root / filename) != binding:
            raise FinalMasterAcceptanceError(
                f"final_master_acceptance_upstream_identity_mismatch:{key}"
            )
    return document


def require_certified_final_video(report_path: Path, final_path: Path) -> dict[str, Any]:
    """Verify a staged/renamed video still matches the exact P4-certified bytes."""
    document = _read_object(Path(report_path))
    acceptance = _validate_contract_document(document)
    stored = acceptance["sources"].get("final")
    if not isinstance(stored, dict):
        raise FinalMasterAcceptanceError("final_master_acceptance_missing_final_binding")
    current = _regular_file_binding(Path(final_path))
    if (
        stored.get("sha256") != current.get("sha256")
        or int(stored.get("byte_length") or 0) != int(current.get("byte_length") or -1)
    ):
        raise FinalMasterAcceptanceError("final_master_acceptance_final_video_mismatch")
    return document


def final_master_acceptance_sha256(output_dir: Path) -> str:
    document = require_final_master_acceptance(Path(output_dir))
    return str(document["acceptance_contract"]["sources"]["final"]["sha256"])

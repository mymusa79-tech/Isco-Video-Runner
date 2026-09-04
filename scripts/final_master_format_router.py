from __future__ import annotations

"""Format-aware runtime router above the byte-certified Final Master QC core.

Long-form remains delegated to the certified core unchanged. Moment/Short uses the
same certified probing, stream contract, detector implementations, thresholds, and
blocking semantics, but resolves body duration from the artifact family that the
Short path actually owns.
"""

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from scripts import final_master_qc as core
from scripts.final_master_body_contract import (
    FinalMasterBodyContractError,
    resolve_body_contract,
)
from scripts.runtime_phase import canonical_runtime_enabled
from scripts.runtime_reliability import production_entrypoint_modules


ROUTER_ID = "final-master-format-router-v1"
ROUTER_VERSION = 1
DURATION_TOLERANCE_SECONDS = 0.25


def implementation_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _format(plan: dict[str, Any], quality: dict[str, Any]) -> str:
    return str(plan.get("format") or quality.get("format") or "").strip().lower()


def _moment_report(
    root: Path,
    *,
    plan: dict[str, Any],
    quality: dict[str, Any],
) -> dict[str, Any]:
    final_path = root / "final.mp4"
    report_path = root / "final-master-qc.json"
    if not final_path.is_file():
        raise core.FinalMasterQCError("Final Master QC requires final.mp4")

    try:
        body_contract = resolve_body_contract(root, fmt="moment", quality=quality)
    except FinalMasterBodyContractError as exc:
        raise core.FinalMasterQCError(str(exc)) from exc

    body_end = core._float(body_contract.get("duration_seconds"))
    if body_end <= 0:
        raise core.FinalMasterQCError("Final Master QC requires positive body duration")

    info = core.probe(final_path)
    final_seconds = (
        core._float((info.get("format") or {}).get("duration"))
        if isinstance(info, dict)
        else 0.0
    )
    if final_seconds <= 0:
        raise core.FinalMasterQCError("Final Master QC found invalid final/body duration relationship")
    if abs(final_seconds - body_end) > DURATION_TOLERANCE_SECONDS:
        raise core.FinalMasterQCError("Final Master QC found Moment final/body duration mismatch")

    stream_contract, blocking, warnings = core._stream_contract(info, "moment")
    has_audio = bool(stream_contract["audio_stream_count"])
    scan = core._run_full_scan(final_path, has_audio=has_audio)
    stderr = str(scan.pop("stderr", ""))
    scan["silence_events"] = (
        core._parse_silence_events(stderr, final_seconds=final_seconds)
        if has_audio
        else []
    )
    scan["freeze_events"] = core._parse_freeze_events(stderr, final_seconds=final_seconds)

    if scan.get("timed_out") is True:
        blocking.append("full_decode_timeout")
    elif scan["returncode"] != 0:
        blocking.append("full_decode_failed")

    for event in scan["black_events"]:
        overlap = core._interior_overlap(event, body_end=body_end)
        if overlap >= core.BLACK_DETECT_SECONDS:
            blocking.append(
                f"interior_near_black={event['start_seconds']:.3f}-{event['end_seconds']:.3f}s"
            )

    for event in scan["silence_events"]:
        overlap = core._interior_overlap(event, body_end=body_end)
        if overlap >= core.SILENCE_DETECT_SECONDS:
            blocking.append(
                f"interior_audio_dropout={event['start_seconds']:.3f}-{event['end_seconds']:.3f}s"
            )

    freeze_warnings: list[dict[str, float]] = []
    for event in scan["freeze_events"]:
        overlap = core._interior_overlap(event, body_end=body_end)
        if overlap >= core.FREEZE_BLOCK_SECONDS:
            blocking.append(
                f"interior_exact_freeze={event['start_seconds']:.3f}-{event['end_seconds']:.3f}s"
            )
        elif overlap >= core.FREEZE_DETECT_SECONDS:
            freeze_warnings.append(
                {**event, "interior_overlap_seconds": round(overlap, 3)}
            )
    if freeze_warnings:
        warnings.append("interior_exact_freeze_observed_below_block_threshold")

    report: dict[str, Any] = {
        "schema_version": core.SCHEMA_VERSION,
        "status": "block" if blocking else "pass",
        "production_stage": "post_render_pre_gold_acceptance",
        "final_file": final_path.name,
        "format": "moment",
        "final_duration_seconds": round(final_seconds, 3),
        "body_duration_seconds": round(body_end, 3),
        "body_duration_source": body_contract["source"],
        "body_contract_kind": body_contract["kind"],
        "m7_timeline_authoritative": False,
        "short_timeline_authoritative": bool(
            body_contract["short_timeline_authoritative"]
        ),
        "quality_duration_crosscheck_seconds": (
            round(float(body_contract["quality_duration_crosscheck_seconds"]), 3)
            if body_contract.get("quality_duration_crosscheck_seconds") is not None
            else None
        ),
        "m7_body_duration_seconds": round(body_end, 3),
        "outro_or_tail_seconds": round(max(0.0, final_seconds - body_end), 3),
        "full_decode_ok": scan["returncode"] == 0
        and scan.get("timed_out") is not True,
        "full_decode_timed_out": scan.get("timed_out") is True,
        "full_decode_timeout_seconds": core.FULL_SCAN_TIMEOUT_SECONDS,
        "stream_contract": stream_contract,
        "detectors": {
            "near_black": {
                "minimum_seconds": core.BLACK_DETECT_SECONDS,
                "pixel_threshold": core.BLACK_PIXEL_THRESHOLD,
                "picture_ratio": core.BLACK_PICTURE_RATIO,
                "events": scan["black_events"],
            },
            "audio_silence": {
                "minimum_seconds": core.SILENCE_DETECT_SECONDS,
                "threshold_db": core.SILENCE_THRESHOLD_DB,
                "events": scan["silence_events"],
            },
            "exact_freeze": {
                "observe_seconds": core.FREEZE_DETECT_SECONDS,
                "block_seconds": core.FREEZE_BLOCK_SECONDS,
                "events": scan["freeze_events"],
                "below_block_threshold": freeze_warnings,
            },
        },
        "opening_grace_seconds": core.OPENING_GRACE_SECONDS,
        "outro_excluded_from_perceptual_blocks": True,
        "blocking_findings": blocking,
        "warnings": warnings,
        "existing_quality_authorities_preserved": [
            "engine_quality_final_duration",
            "engine_quality_final_loudness_true_peak",
            "engine_quality_final_av_sync",
            "gold_final_critic",
        ],
        "format_router": {
            "id": ROUTER_ID,
            "version": ROUTER_VERSION,
            "certified_core_delegation": False,
            "certified_core_policy_surface_reused": True,
        },
        "ai_calls_added": 0,
        "final_media_mutated": False,
    }
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if blocking:
        raise core.FinalMasterQCError(
            "Final Master QC blocked release; inspect final-master-qc.json"
        )
    return report


def run_format_aware_final_master_qc(
    output_dir: Path,
    *,
    original: Callable[[Path], dict[str, Any]],
) -> dict[str, Any]:
    root = Path(output_dir)
    plan = core._read_object(root / "plan.json")
    quality = core._read_object(root / "quality-final.json")
    fmt = _format(plan, quality)
    if fmt != "moment":
        return original(root)
    return _moment_report(root, plan=plan, quality=quality)


def install_format_aware_qc_router() -> None:
    """Wrap only live production QC ports; never mutate the certified core symbol.

    Runtime closure calls this immediately before Final-QC durability is installed.
    Durability therefore captures this wrapper as its `original`, yielding the explicit
    order Durable QC -> Format Router -> certified core. Direct core unit tests and the
    byte-certified stable port remain untouched.
    """
    for production in production_entrypoint_modules():
        current = getattr(production, "run_final_master_qc", None)
        if not callable(current) or getattr(
            current, "_isco_format_aware_final_master_qc", False
        ) is True:
            continue

        def make_wrapper(original: Callable[[Path], dict[str, Any]]):
            def wrapped(output_dir: Path) -> dict[str, Any]:
                if not canonical_runtime_enabled():
                    return original(Path(output_dir))
                return run_format_aware_final_master_qc(
                    Path(output_dir),
                    original=original,
                )

            wrapped._isco_format_aware_final_master_qc = True
            wrapped._isco_format_aware_final_master_qc_original = original
            return wrapped

        setattr(production, "run_final_master_qc", make_wrapper(current))

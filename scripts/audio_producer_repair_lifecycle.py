from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from functools import wraps
from pathlib import Path
from typing import Any, Callable

import isco_video_agent.orchestrator as orchestrator
from isco_video_agent.config import load_channel_config
from isco_video_agent.media.ffmpeg import measure_audio_loudness, probe
from isco_video_agent.security import secret_free_subprocess_env

from scripts import short_voice_v2


REPORT_FILENAME = "audio-producer-repair.json"
SCHEMA_VERSION = 1
GATE_LUFS_TOLERANCE = 2.5
GATE_TRUE_PEAK_DBTP = -1.0
CORRECTION_TRUE_PEAK_DBTP = -1.5
MAX_REPAIRABLE_LUFS_DELTA = 6.0
MAX_REPAIRABLE_TRUE_PEAK_DBTP = 0.0
MAX_STREAM_DURATION_DRIFT_SECONDS = 0.08
ALIMITER_CEILING_LINEAR = 0.84
_INSTALLED = False


class AudioProducerRepairError(RuntimeError):
    """Producer-owned pre-gate audio acceptance/repair failure."""


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stream_duration(streams: list[dict[str, Any]]) -> float:
    if not streams:
        return 0.0
    return _safe_float(streams[0].get("duration"), 0.0)


def _media_state(path: Path) -> dict[str, Any]:
    info = probe(path)
    streams = info.get("streams") if isinstance(info, dict) else []
    video = [x for x in (streams or []) if isinstance(x, dict) and x.get("codec_type") == "video"]
    audio = [x for x in (streams or []) if isinstance(x, dict) and x.get("codec_type") == "audio"]
    video_seconds = _stream_duration(video)
    audio_seconds = _stream_duration(audio)
    return {
        "video_streams": len(video),
        "audio_streams": len(audio),
        "video_seconds": video_seconds,
        "audio_seconds": audio_seconds,
        "av_delta_seconds": abs(video_seconds - audio_seconds) if video and audio else None,
        "video_codec": str(video[0].get("codec_name") or "") if video else "",
    }


def _measurement_passes(measurement: dict[str, Any], target_lufs: float) -> bool:
    integrated = _safe_float(measurement.get("integrated_lufs"), 999.0)
    true_peak = _safe_float(measurement.get("true_peak_dbtp"), 999.0)
    return (
        abs(integrated - float(target_lufs)) <= GATE_LUFS_TOLERANCE
        and true_peak <= GATE_TRUE_PEAK_DBTP
    )


def _measurement_repairable(measurement: dict[str, Any], target_lufs: float) -> bool:
    integrated = _safe_float(measurement.get("integrated_lufs"), 999.0)
    true_peak = _safe_float(measurement.get("true_peak_dbtp"), 999.0)
    return (
        abs(integrated - float(target_lufs)) <= MAX_REPAIRABLE_LUFS_DELTA
        and true_peak <= MAX_REPAIRABLE_TRUE_PEAK_DBTP
    )


def _video_stream_hash(path: Path) -> str:
    proc = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path),
            "-map", "0:v:0", "-c", "copy", "-f", "hash", "-hash", "sha256", "-",
        ],
        check=True,
        env=secret_free_subprocess_env(),
        capture_output=True,
        text=True,
    )
    match = re.search(r"SHA256=([0-9a-fA-F]{64})", proc.stdout)
    if not match:
        raise AudioProducerRepairError("audio_producer_video_hash_unavailable")
    return match.group(1).lower()


def _loudnorm_analysis(path: Path, target_lufs: float) -> dict[str, str]:
    proc = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
            "-map", "0:a:0",
            "-af", f"loudnorm=I={target_lufs}:TP={CORRECTION_TRUE_PEAK_DBTP}:LRA=11:print_format=json",
            "-f", "null", "-",
        ],
        check=True,
        env=secret_free_subprocess_env(),
        capture_output=True,
        text=True,
    )
    blocks = re.findall(r"\{\s*\"input_i\".*?\}", proc.stderr, flags=re.S)
    if not blocks:
        raise AudioProducerRepairError("audio_producer_loudnorm_analysis_unparseable")
    data = json.loads(blocks[-1])
    required = ("input_i", "input_tp", "input_lra", "input_thresh")
    if any(key not in data for key in required):
        raise AudioProducerRepairError("audio_producer_loudnorm_analysis_incomplete")
    return {
        "measured_i": str(data["input_i"]),
        "measured_tp": str(data["input_tp"]),
        "measured_lra": str(data["input_lra"]),
        "measured_thresh": str(data["input_thresh"]),
        "offset": str(data.get("target_offset", "0")),
    }


def _corrective_filter(target_lufs: float, measured: dict[str, str]) -> str:
    return (
        f"loudnorm=I={target_lufs}:TP={CORRECTION_TRUE_PEAK_DBTP}:LRA=11:"
        f"measured_I={measured['measured_i']}:measured_TP={measured['measured_tp']}:"
        f"measured_LRA={measured['measured_lra']}:measured_thresh={measured['measured_thresh']}:"
        f"offset={measured['offset']}:linear=true,"
        f"alimiter=limit={ALIMITER_CEILING_LINEAR}:level=disabled,aresample=48000"
    )


def _render_corrected_candidate(path: Path, target_lufs: float) -> tuple[Path, dict[str, Any]]:
    source_state = _media_state(path)
    if source_state["video_streams"] != 1 or source_state["audio_streams"] != 1:
        raise AudioProducerRepairError("audio_producer_repair_requires_one_video_one_audio_stream")
    source_video_hash = _video_stream_hash(path)
    measured = _loudnorm_analysis(path, target_lufs)
    candidate = path.with_name(path.stem + ".audio-producer-candidate" + path.suffix)
    candidate.unlink(missing_ok=True)
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(path),
                "-map", "0:v:0", "-map", "0:a:0", "-map_metadata", "0",
                "-c:v", "copy", "-af", _corrective_filter(target_lufs, measured),
                "-c:a", "aac", "-ar", "48000", "-b:a", "192k", "-movflags", "+faststart",
                str(candidate),
            ],
            check=True,
            env=secret_free_subprocess_env(),
            capture_output=True,
        )
        if not candidate.is_file() or candidate.stat().st_size <= 1024:
            raise AudioProducerRepairError("audio_producer_corrected_candidate_missing")
        candidate_state = _media_state(candidate)
        if candidate_state["video_streams"] != 1 or candidate_state["audio_streams"] != 1:
            raise AudioProducerRepairError("audio_producer_corrected_candidate_stream_shape_changed")
        if candidate_state["video_codec"] != source_state["video_codec"]:
            raise AudioProducerRepairError("audio_producer_corrected_candidate_video_codec_changed")
        if abs(candidate_state["video_seconds"] - source_state["video_seconds"]) > MAX_STREAM_DURATION_DRIFT_SECONDS:
            raise AudioProducerRepairError("audio_producer_corrected_candidate_video_duration_changed")
        if abs(candidate_state["audio_seconds"] - source_state["audio_seconds"]) > MAX_STREAM_DURATION_DRIFT_SECONDS:
            raise AudioProducerRepairError("audio_producer_corrected_candidate_audio_duration_changed")
        if _video_stream_hash(candidate) != source_video_hash:
            raise AudioProducerRepairError("audio_producer_corrected_candidate_video_stream_changed")
        after = measure_audio_loudness(candidate)
        if not _measurement_passes(after, target_lufs):
            raise AudioProducerRepairError("audio_producer_repair_exhausted_after_one_correction")
        os.replace(candidate, path)
        return path, {"measurement": after, "media_state": candidate_state}
    finally:
        candidate.unlink(missing_ok=True)


def _read_report(root: Path) -> dict[str, Any]:
    path = root / REPORT_FILENAME
    if not path.is_file():
        return {"schema_version": SCHEMA_VERSION, "receipts": []}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"schema_version": SCHEMA_VERSION, "receipts": []}
    if not isinstance(value, dict):
        return {"schema_version": SCHEMA_VERSION, "receipts": []}
    return value


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _write_receipt(root: Path, receipt: dict[str, Any], *, final_path: Path) -> None:
    final_path = Path(final_path)
    if not final_path.is_file():
        raise AudioProducerRepairError("audio_producer_receipt_final_missing")
    bound = dict(receipt)
    bound["final_sha256"] = _sha256_file(final_path)
    path = root / REPORT_FILENAME
    report = _read_report(root)
    phase = str(bound.get("phase") or "")
    receipts = [
        item for item in list(report.get("receipts") or [])
        if isinstance(item, dict) and str(item.get("phase") or "") != phase
    ]
    receipts.append(bound)
    report.update({"schema_version": SCHEMA_VERSION, "receipts": receipts})
    _atomic_json(path, report)


def resolve_audio_producer_handoff(
    final_path: Path,
    *,
    phase: str,
    target_lufs: float,
    expected_audio: bool,
    measure_fn: Callable[[Path], dict[str, Any]] = measure_audio_loudness,
    state_fn: Callable[[Path], dict[str, Any]] = _media_state,
    repair_fn: Callable[[Path, float], tuple[Path, dict[str, Any]]] = _render_corrected_candidate,
) -> dict[str, Any]:
    """Certify, repair one owned loudness defect, or fail closed before final gates."""
    final_path = Path(final_path)
    root = final_path.parent
    if not final_path.is_file():
        raise AudioProducerRepairError("audio_producer_final_missing")
    state = state_fn(final_path)
    if state["video_streams"] != 1:
        raise AudioProducerRepairError("audio_producer_requires_exactly_one_video_stream")
    if state["audio_streams"] == 0 and not expected_audio:
        receipt = {
            "phase": phase,
            "decision": "not_applicable",
            "repair_attempts": 0,
            "reason": "audio_not_required",
        }
        _write_receipt(root, receipt, final_path=final_path)
        return {**receipt, "final_sha256": _sha256_file(final_path)}
    if state["audio_streams"] != 1:
        receipt = {
            "phase": phase,
            "decision": "block",
            "repair_attempts": 0,
            "reason": "audio_stream_shape_unowned",
            "media_state": state,
        }
        _write_receipt(root, receipt, final_path=final_path)
        raise AudioProducerRepairError("audio_producer_audio_stream_shape_unowned")

    av_limit = float(load_channel_config()["quality"].get("av_sync_max_delta_seconds", 1.0))
    if _safe_float(state.get("av_delta_seconds"), 999.0) > av_limit:
        receipt = {
            "phase": phase,
            "decision": "block",
            "repair_attempts": 0,
            "reason": "av_sync_unowned",
            "av_sync_limit_seconds": av_limit,
            "media_state": state,
        }
        _write_receipt(root, receipt, final_path=final_path)
        raise AudioProducerRepairError("audio_producer_av_sync_unowned_fail_closed")

    before = measure_fn(final_path)
    if _measurement_passes(before, target_lufs):
        receipt = {
            "phase": phase,
            "decision": "pass",
            "repair_attempts": 0,
            "target_lufs": float(target_lufs),
            "measurement_before": before,
            "measurement_after": before,
            "media_state": state,
        }
        _write_receipt(root, receipt, final_path=final_path)
        return {**receipt, "final_sha256": _sha256_file(final_path)}

    if not _measurement_repairable(before, target_lufs):
        receipt = {
            "phase": phase,
            "decision": "block",
            "repair_attempts": 0,
            "reason": "loudness_outside_owned_repair_envelope",
            "target_lufs": float(target_lufs),
            "measurement_before": before,
            "media_state": state,
        }
        _write_receipt(root, receipt, final_path=final_path)
        raise AudioProducerRepairError("audio_producer_loudness_unowned_fail_closed")

    try:
        _path, repaired = repair_fn(final_path, float(target_lufs))
    except Exception as exc:
        receipt = {
            "phase": phase,
            "decision": "block",
            "repair_attempts": 1,
            "reason": "owned_repair_failed",
            "target_lufs": float(target_lufs),
            "measurement_before": before,
            "error": f"{type(exc).__name__}: {str(exc)[:500]}",
        }
        _write_receipt(root, receipt, final_path=final_path)
        if isinstance(exc, AudioProducerRepairError):
            raise
        raise AudioProducerRepairError("audio_producer_owned_repair_failed") from exc

    after = repaired.get("measurement") if isinstance(repaired, dict) else None
    after_state = repaired.get("media_state") if isinstance(repaired, dict) else None
    if not isinstance(after, dict) or not _measurement_passes(after, target_lufs):
        receipt = {
            "phase": phase,
            "decision": "block",
            "repair_attempts": 1,
            "reason": "owned_repair_revalidation_failed",
            "target_lufs": float(target_lufs),
            "measurement_before": before,
            "measurement_after": after,
        }
        _write_receipt(root, receipt, final_path=final_path)
        raise AudioProducerRepairError("audio_producer_repair_revalidation_failed")

    receipt = {
        "phase": phase,
        "decision": "repaired_pass",
        "repair_attempts": 1,
        "target_lufs": float(target_lufs),
        "measurement_before": before,
        "measurement_after": after,
        "media_state_before": state,
        "media_state_after": after_state,
        "repair_owner": "audio_producer_repair_lifecycle",
        "independent_gate_still_required": True,
    }
    _write_receipt(root, receipt, final_path=final_path)
    bound = {**receipt, "final_sha256": _sha256_file(final_path)}
    print(
        "Audio Producer repair PASS: "
        f"phase={phase} attempts=1 target_lufs={float(target_lufs):.1f}"
    )
    return bound


def _install_core_mux_wrapper() -> None:
    current = orchestrator.mux
    if getattr(current, "_isco_audio_producer_repair", False):
        return

    @wraps(current)
    def wrapped(video, narration, output, *args, **kwargs):
        result = current(video, narration, output, *args, **kwargs)
        final_path = Path(result if result is not None else output)
        music = kwargs.get("music")
        expected_audio = narration is not None or music is not None
        if not expected_audio:
            resolve_audio_producer_handoff(
                final_path,
                phase="core_mux",
                target_lufs=-18.0,
                expected_audio=False,
            )
            return result
        target = _safe_float(kwargs.get("target_lufs"), -16.0) if narration is not None else -18.0
        resolve_audio_producer_handoff(
            final_path,
            phase="core_mux",
            target_lufs=target,
            expected_audio=True,
        )
        return result

    wrapped._isco_audio_producer_repair = True
    wrapped._isco_audio_producer_repair_original = current
    orchestrator.mux = wrapped


def _install_short_finished_wrapper() -> None:
    current = short_voice_v2._refresh_quality_final
    if getattr(current, "_isco_audio_producer_repair", False):
        return

    @wraps(current)
    def wrapped(root: Path, final_path: Path):
        target = float(load_channel_config()["quality"].get("audio_lufs", -16))
        resolve_audio_producer_handoff(
            Path(final_path),
            phase="short_finished",
            target_lufs=target,
            expected_audio=True,
        )
        return current(root, final_path)

    wrapped._isco_audio_producer_repair = True
    wrapped._isco_audio_producer_repair_original = current
    short_voice_v2._refresh_quality_final = wrapped


def install_audio_producer_repair_lifecycle() -> None:
    """Install capability-owned audio pre-gate repair for Long + finished Shorts."""
    global _INSTALLED
    if _INSTALLED:
        return
    _install_core_mux_wrapper()
    _install_short_finished_wrapper()
    _INSTALLED = True
    print(
        "Audio Producer Repair Lifecycle installed: measure -> one bounded loudness/peak correction -> "
        "re-measure; A/V sync/provenance/semantic/security defects fail closed"
    )

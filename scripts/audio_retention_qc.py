from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
import tempfile
import wave
from array import array
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

try:
    from isco_video_agent.security import secret_free_subprocess_env
except ImportError:  # pragma: no cover - test/import fallback before Engine path is installed
    def secret_free_subprocess_env() -> dict[str, str]:
        import os
        return dict(os.environ)


SCHEMA_VERSION = 1
REPORT_FILENAME = "audio-retention-qc.json"
ANALYSIS_SAMPLE_RATE = 16000
FRAME_MS = 20
GAIN_WINDOW_MS = 250
SILENCE_THRESHOLD_DBFS = -52.0
CLIP_AMPLITUDE = 32760
CLICK_DELTA = 30000
MAX_REPORTED_EVENTS = 24


class AudioRetentionQCError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RetentionProfile:
    name: str
    opening_silence_block_seconds: float
    interior_silence_block_seconds: float
    clipped_sample_ratio_block: float
    clipped_run_block_ms: float
    gain_jump_warn_db: float
    click_rate_warn_per_minute: float
    quiet_floor_warn_dbfs: float


SHORT_PROFILE = RetentionProfile(
    name="short_retention_v1",
    opening_silence_block_seconds=0.55,
    interior_silence_block_seconds=1.25,
    clipped_sample_ratio_block=0.0020,
    clipped_run_block_ms=8.0,
    gain_jump_warn_db=11.0,
    click_rate_warn_per_minute=10.0,
    quiet_floor_warn_dbfs=-38.0,
)
LONG_PROFILE = RetentionProfile(
    name="long_retention_v1",
    opening_silence_block_seconds=4.0,
    interior_silence_block_seconds=4.0,
    clipped_sample_ratio_block=0.0030,
    clipped_run_block_ms=12.0,
    gain_jump_warn_db=13.0,
    click_rate_warn_per_minute=14.0,
    quiet_floor_warn_dbfs=-36.0,
)


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise AudioRetentionQCError(f"audio_retention_invalid_json:{Path(path).name}") from exc
    if not isinstance(value, dict):
        raise AudioRetentionQCError(f"audio_retention_wrong_shape:{Path(path).name}")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _dbfs_from_mean_square(mean_square: float) -> float:
    if mean_square <= 0.0:
        return -120.0
    rms = math.sqrt(mean_square) / 32768.0
    return max(-120.0, 20.0 * math.log10(max(rms, 1e-12)))


def _window_dbfs(samples: list[int] | array, start: int, end: int) -> float:
    if end <= start:
        return -120.0
    total = 0.0
    count = 0
    for value in samples[start:end]:
        total += float(value) * float(value)
        count += 1
    return _dbfs_from_mean_square(total / max(1, count))


def _silence_events(samples: list[int] | array, sample_rate: int) -> list[dict[str, float]]:
    frame = max(1, int(round(sample_rate * FRAME_MS / 1000.0)))
    states: list[tuple[int, int, bool]] = []
    for start in range(0, len(samples), frame):
        end = min(len(samples), start + frame)
        states.append((start, end, _window_dbfs(samples, start, end) <= SILENCE_THRESHOLD_DBFS))

    result: list[dict[str, float]] = []
    open_start: int | None = None
    open_end = 0
    for start, end, silent in states:
        if silent and open_start is None:
            open_start = start
            open_end = end
        elif silent:
            open_end = end
        elif open_start is not None:
            result.append(_event_samples(open_start, open_end, sample_rate))
            open_start = None
    if open_start is not None:
        result.append(_event_samples(open_start, open_end, sample_rate))
    return result


def _event_samples(start: int, end: int, sample_rate: int) -> dict[str, float]:
    start_s = start / sample_rate
    end_s = end / sample_rate
    return {
        "start_seconds": round(start_s, 3),
        "end_seconds": round(end_s, 3),
        "duration_seconds": round(max(0.0, end_s - start_s), 3),
    }


def _clipping(samples: list[int] | array, sample_rate: int) -> dict[str, Any]:
    if not samples:
        return {"sample_ratio": 0.0, "longest_run_ms": 0.0, "peak_dbfs": -120.0}
    clipped = 0
    run = 0
    longest = 0
    peak = 0
    for value in samples:
        absolute = abs(int(value))
        peak = max(peak, absolute)
        if absolute >= CLIP_AMPLITUDE:
            clipped += 1
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    peak_dbfs = -120.0 if peak <= 0 else 20.0 * math.log10(min(1.0, peak / 32768.0))
    return {
        "sample_ratio": round(clipped / len(samples), 8),
        "clipped_samples": clipped,
        "longest_run_ms": round(longest * 1000.0 / sample_rate, 3),
        "peak_dbfs": round(peak_dbfs, 4),
    }


def _click_events(samples: list[int] | array, sample_rate: int) -> list[float]:
    events: list[float] = []
    refractory = max(1, int(round(sample_rate * 0.004)))
    last_index = -refractory
    for index in range(1, len(samples)):
        if index - last_index < refractory:
            continue
        if abs(int(samples[index]) - int(samples[index - 1])) >= CLICK_DELTA:
            events.append(round(index / sample_rate, 3))
            last_index = index
            if len(events) >= MAX_REPORTED_EVENTS:
                break
    return events


def _gain_jumps(samples: list[int] | array, sample_rate: int, *, threshold_db: float) -> list[dict[str, float]]:
    size = max(1, int(round(sample_rate * GAIN_WINDOW_MS / 1000.0)))
    levels: list[tuple[float, float]] = []
    for start in range(0, len(samples), size):
        end = min(len(samples), start + size)
        levels.append((start / sample_rate, _window_dbfs(samples, start, end)))
    events: list[dict[str, float]] = []
    for (_time_a, level_a), (time_b, level_b) in zip(levels, levels[1:]):
        if min(level_a, level_b) <= SILENCE_THRESHOLD_DBFS + 4.0:
            continue
        delta = abs(level_b - level_a)
        if delta >= threshold_db:
            events.append(
                {
                    "at_seconds": round(time_b, 3),
                    "before_dbfs": round(level_a, 3),
                    "after_dbfs": round(level_b, 3),
                    "delta_db": round(delta, 3),
                }
            )
            if len(events) >= MAX_REPORTED_EVENTS:
                break
    return events


def _quiet_floor(samples: list[int] | array, sample_rate: int) -> dict[str, Any]:
    size = max(1, int(round(sample_rate * GAIN_WINDOW_MS / 1000.0)))
    levels = [
        _window_dbfs(samples, start, min(len(samples), start + size))
        for start in range(0, len(samples), size)
    ]
    candidates = [level for level in levels if SILENCE_THRESHOLD_DBFS < level <= -24.0]
    if not candidates:
        return {"available": False, "p20_dbfs": None, "candidate_windows": 0}
    ordered = sorted(candidates)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * 0.20))))
    return {
        "available": True,
        "p20_dbfs": round(ordered[index], 3),
        "candidate_windows": len(candidates),
    }


def analyze_pcm(samples: list[int] | array, sample_rate: int, profile: RetentionProfile) -> dict[str, Any]:
    if sample_rate <= 0:
        raise AudioRetentionQCError("audio_retention_invalid_sample_rate")
    if not samples:
        raise AudioRetentionQCError("audio_retention_empty_pcm")

    duration = len(samples) / sample_rate
    global_level = _window_dbfs(samples, 0, len(samples))
    silences = _silence_events(samples, sample_rate)
    clipping = _clipping(samples, sample_rate)
    clicks = _click_events(samples, sample_rate)
    gain_jumps = _gain_jumps(samples, sample_rate, threshold_db=profile.gain_jump_warn_db)
    quiet_floor = _quiet_floor(samples, sample_rate)

    blocking: list[str] = []
    warnings: list[str] = []
    opening = next((event for event in silences if event["start_seconds"] <= 0.021), None)
    if opening and opening["duration_seconds"] >= profile.opening_silence_block_seconds:
        blocking.append(
            f"opening_audio_dropout={opening['duration_seconds']:.3f}s;max={profile.opening_silence_block_seconds:.3f}s"
        )
    interior = [
        event
        for event in silences
        if event["start_seconds"] > 0.021
        and event["end_seconds"] < max(0.0, duration - 0.15)
        and event["duration_seconds"] >= profile.interior_silence_block_seconds
    ]
    for event in interior[:MAX_REPORTED_EVENTS]:
        blocking.append(
            f"interior_audio_dropout={event['start_seconds']:.3f}-{event['end_seconds']:.3f}s"
        )
    if clipping["sample_ratio"] >= profile.clipped_sample_ratio_block:
        blocking.append(
            f"sustained_clipping_ratio={clipping['sample_ratio']:.6f};max={profile.clipped_sample_ratio_block:.6f}"
        )
    if clipping["longest_run_ms"] >= profile.clipped_run_block_ms:
        blocking.append(
            f"sustained_clipping_run_ms={clipping['longest_run_ms']:.3f};max={profile.clipped_run_block_ms:.3f}"
        )

    click_rate = len(clicks) / max(duration / 60.0, 1e-6)
    if click_rate >= profile.click_rate_warn_per_minute:
        warnings.append(f"click_pop_suspected_rate_per_minute={click_rate:.3f}")
    if gain_jumps:
        warnings.append(f"gain_jump_suspected_count={len(gain_jumps)}")
    floor = quiet_floor.get("p20_dbfs")
    if isinstance(floor, (int, float)) and float(floor) >= profile.quiet_floor_warn_dbfs:
        warnings.append(
            f"elevated_quiet_floor_suspected={float(floor):.3f}dBFS;warn={profile.quiet_floor_warn_dbfs:.3f}dBFS"
        )
    if global_level <= -35.0:
        warnings.append(f"final_mix_unusually_quiet_rms={global_level:.3f}dBFS")

    return {
        "duration_seconds": round(duration, 3),
        "global_rms_dbfs": round(global_level, 3),
        "silence_threshold_dbfs": SILENCE_THRESHOLD_DBFS,
        "silence_events": silences[:MAX_REPORTED_EVENTS],
        "clipping": clipping,
        "click_pop": {
            "suspected_events_seconds": clicks,
            "rate_per_minute": round(click_rate, 3),
            "warning_only": True,
        },
        "gain_jumps": {"events": gain_jumps, "warning_only": True},
        "quiet_floor": {**quiet_floor, "warning_only": True},
        "blocking_findings": blocking,
        "warnings": warnings,
    }


def _profile_for(root: Path) -> tuple[str, RetentionProfile, str]:
    plan = _read_object(root / "plan.json")
    quality = _read_object(root / "quality-final.json")
    fmt = str(plan.get("format") or quality.get("format") or "").strip().lower()
    if fmt not in {"film", "story", "moment"}:
        raise AudioRetentionQCError(f"audio_retention_unsupported_format:{fmt or 'missing'}")
    if fmt == "moment":
        scope = "short"
        short_context = root / "short-intelligence-pre-gold.json"
        if short_context.is_file():
            context = _read_object(short_context)
            compensation = context.get("compensation") if isinstance(context.get("compensation"), dict) else {}
            approval_scope = str(compensation.get("scope") or "").strip()
            if approval_scope == "short_sibling":
                scope = "source_derived_short"
            elif approval_scope == "short_only":
                scope = "standalone_short"
        return fmt, SHORT_PROFILE, scope
    return fmt, LONG_PROFILE, "long"


def _decode_to_wav(final_path: Path, wav_path: Path) -> None:
    if shutil.which("ffmpeg") is None:
        raise AudioRetentionQCError("audio_retention_ffmpeg_missing")
    command = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(final_path),
        "-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(ANALYSIS_SAMPLE_RATE),
        "-c:a", "pcm_s16le", str(wav_path),
    ]
    try:
        subprocess.run(
            command,
            check=True,
            env=secret_free_subprocess_env(),
            capture_output=True,
            text=True,
            timeout=1200,
        )
    except FileNotFoundError as exc:
        raise AudioRetentionQCError("audio_retention_ffmpeg_missing") from exc
    except subprocess.TimeoutExpired as exc:
        raise AudioRetentionQCError("audio_retention_decode_timeout") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip().replace("\n", " ")[:220]
        raise AudioRetentionQCError(f"audio_retention_decode_failed:{detail}") from exc
    if not wav_path.is_file() or wav_path.stat().st_size <= 44:
        raise AudioRetentionQCError("audio_retention_decoded_audio_missing")


def _read_wav_pcm(path: Path) -> tuple[array, int]:
    with wave.open(str(path), "rb") as handle:
        if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            raise AudioRetentionQCError("audio_retention_unexpected_analysis_pcm")
        rate = int(handle.getframerate())
        frames = handle.readframes(handle.getnframes())
    samples = array("h")
    samples.frombytes(frames)
    return samples, rate


def run_audio_retention_qc(
    output_dir: Path,
    *,
    decoder: Callable[[Path, Path], None] = _decode_to_wav,
) -> dict[str, Any]:
    root = Path(output_dir)
    final_path = root / "final.mp4"
    if not final_path.is_file() or final_path.stat().st_size <= 1024:
        raise AudioRetentionQCError("audio_retention_final_missing")
    fmt, profile, scope = _profile_for(root)
    final_sha = _sha256_file(final_path)

    with tempfile.TemporaryDirectory(prefix="isco-audio-retention-") as temp_dir:
        wav_path = Path(temp_dir) / "analysis.wav"
        decoder(final_path, wav_path)
        samples, sample_rate = _read_wav_pcm(wav_path)
        analysis = analyze_pcm(samples, sample_rate, profile)

    if _sha256_file(final_path) != final_sha:
        raise AudioRetentionQCError("audio_retention_final_changed_during_analysis")

    report = {
        "schema_version": SCHEMA_VERSION,
        "contract": "audio.retention.qc.v1",
        "status": "block" if analysis["blocking_findings"] else "pass",
        "production_stage": "exact_byte_audio_preflight_before_semantic_audit",
        "format": fmt,
        "scope": scope,
        "profile": asdict(profile),
        "final": {"file": final_path.name, "sha256": final_sha},
        "analysis_sample_rate": sample_rate,
        "metrics": {
            key: value
            for key, value in analysis.items()
            if key not in {"blocking_findings", "warnings"}
        },
        "blocking_findings": analysis["blocking_findings"],
        "warnings": analysis["warnings"],
        "policy": {
            "objective_dropouts_and_sustained_clipping": "fail_closed",
            "click_pop_gain_jump_quiet_floor": "warning_only_until_calibrated",
            "media_mutation": "forbidden",
            "repair_attempts_added": 0,
            "ai_calls_added": 0,
        },
    }
    (root / REPORT_FILENAME).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def require_audio_retention_qc(output_dir: Path) -> dict[str, Any]:
    report = run_audio_retention_qc(output_dir)
    if report.get("status") != "pass":
        findings = list(report.get("blocking_findings") or [])
        detail = findings[0] if findings else "unknown"
        raise AudioRetentionQCError(f"audio_retention_qc_blocked:{detail}")
    return report

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from .media import probe_duration


# Same two-pass loudnorm + limiter targets and methodology as the Engine's own
# certified mux() (isco_video_agent.media.ffmpeg): analyze real pre-final audio,
# then apply a linear corrective loudnorm using that measurement, followed by an
# alimiter with level=disabled (auto makeup-gain otherwise renormalizes the signal
# back up after limiting, silently undoing the loudnorm correction above it).
TARGET_INTEGRATED_LUFS = -16.0
TARGET_TRUE_PEAK_DBTP = -1.5
TARGET_LOUDNESS_RANGE = 11.0
ALIMITER_CEILING_LINEAR = 0.84
MAX_DURATION_DRIFT_SECONDS = 0.08

_LOUDNORM_JSON_RE = re.compile(r"\{\s*\"input_i\".*?\}", re.S)


def _measure_loudness(path: Path) -> dict[str, Any]:
    proc = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-af",
            (
                f"loudnorm=I={TARGET_INTEGRATED_LUFS}:TP={TARGET_TRUE_PEAK_DBTP}:"
                f"LRA={TARGET_LOUDNESS_RANGE}:print_format=json"
            ),
            "-f",
            "null",
            "-",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    blocks = _LOUDNORM_JSON_RE.findall(proc.stderr)
    if not blocks:
        raise RuntimeError("audio_loudness_measurement_unparseable")
    return json.loads(blocks[-1])


def master_narration_loudness(src: Path, dest: Path) -> dict[str, Any]:
    """Apply a genuine two-pass loudnorm + limiter to narration audio.

    Mirrors the Engine's own certified mux() loudness pass (same targets, same
    two-pass measure-then-correct methodology, same alimiter level=disabled fix)
    but on the single Clean V2 narration track directly, since Clean V2 has no
    music/SFX bed to mix ahead of this step.
    """
    src = Path(src)
    dest = Path(dest)
    if not src.is_file():
        raise RuntimeError("audio_loudness_source_missing")
    before = probe_duration(src)
    measured = _measure_loudness(src)
    corrective = (
        f"loudnorm=I={TARGET_INTEGRATED_LUFS}:TP={TARGET_TRUE_PEAK_DBTP}:"
        f"LRA={TARGET_LOUDNESS_RANGE}:measured_I={measured['input_i']}:"
        f"measured_TP={measured['input_tp']}:measured_LRA={measured['input_lra']}:"
        f"measured_thresh={measured['input_thresh']}:"
        f"offset={measured.get('target_offset', '0')}:linear=true,"
        f"alimiter=limit={ALIMITER_CEILING_LINEAR}:level=disabled,aresample=48000"
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(src),
            "-af",
            corrective,
            "-c:a",
            "pcm_s16le",
            str(dest),
        ],
        check=True,
        timeout=120,
    )
    if not dest.is_file() or dest.stat().st_size <= 0:
        raise RuntimeError("audio_loudness_output_missing_or_empty")
    after = probe_duration(dest)
    if abs(before - after) > MAX_DURATION_DRIFT_SECONDS:
        dest.unlink(missing_ok=True)
        raise RuntimeError(
            f"audio_loudness_duration_drift:{before:.3f}->{after:.3f}"
        )
    return {
        "status": "pass",
        "target_integrated_lufs": TARGET_INTEGRATED_LUFS,
        "target_true_peak_dbtp": TARGET_TRUE_PEAK_DBTP,
        "target_loudness_range": TARGET_LOUDNESS_RANGE,
        "alimiter_ceiling_linear": ALIMITER_CEILING_LINEAR,
        "measured_input_integrated_lufs": float(measured["input_i"]),
        "measured_input_true_peak_dbtp": float(measured["input_tp"]),
    }

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from .media import probe_duration


TARGET_INTEGRATED_LUFS = -16.0
TARGET_TRUE_PEAK_DBTP = -1.5
TARGET_LOUDNESS_RANGE = 11.0
ALIMITER_CEILING_LINEAR = 0.84
MAX_DURATION_DRIFT_SECONDS = 0.08

# Charon keeps the previously certified light corrective chain.
CHARON_CORRECTIVE_PROFILE = "audio-mastering-lite-charon-v1"
CHARON_CORRECTIVE_FILTER = (
    "highpass=f=70,"
    "equalizer=f=220:t=q:w=0.8:g=-1,"
    "equalizer=f=3200:t=q:w=0.9:g=0.8,"
    "deesser=i=0.12:m=0.25:f=0.50:s=o,"
    "acompressor=threshold=0.125:ratio=1.6:attack=25:release=180:"
    "makeup=1.0:knee=2.5:mix=0.80"
)

# The listener-approved Nabra sample used loudness normalization/resampling only.
# Do not put Nabra through the Charon-specific EQ/de-esser/compressor chain.
NABRA_MASTERING_PROFILE = "nabra-loudness-only-v1"
NABRA_CORRECTIVE_FILTER = ""

_LOUDNORM_JSON_RE = re.compile(r"\{\s*\"input_i\".*?\}", re.S)


def _measure_loudness(path: Path, *, prefilter: str = "") -> dict[str, Any]:
    loudnorm = (
        f"loudnorm=I={TARGET_INTEGRATED_LUFS}:TP={TARGET_TRUE_PEAK_DBTP}:"
        f"LRA={TARGET_LOUDNESS_RANGE}:print_format=json"
    )
    filter_chain = f"{prefilter},{loudnorm}" if prefilter else loudnorm
    proc = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-af",
            filter_chain,
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


def master_narration_loudness(
    src: Path,
    dest: Path,
    *,
    voice_provider: str = "",
) -> dict[str, Any]:
    """Master without changing tempo/pitch or cross-applying voice coloration.

    Charon retains its certified light corrective EQ/de-esser/compressor.
    Nabra is intentionally loudness-only to preserve the listener-approved
    af_msa / 0.87 timbre and prosody.
    """
    src = Path(src)
    dest = Path(dest)
    if not src.is_file():
        raise RuntimeError("audio_loudness_source_missing")

    is_nabra = str(voice_provider or "").strip() == "nabra:af_msa"
    profile = NABRA_MASTERING_PROFILE if is_nabra else CHARON_CORRECTIVE_PROFILE
    prefilter = NABRA_CORRECTIVE_FILTER if is_nabra else CHARON_CORRECTIVE_FILTER

    before = probe_duration(src)
    measured = _measure_loudness(src, prefilter=prefilter)
    loudnorm = (
        f"loudnorm=I={TARGET_INTEGRATED_LUFS}:TP={TARGET_TRUE_PEAK_DBTP}:"
        f"LRA={TARGET_LOUDNESS_RANGE}:measured_I={measured['input_i']}:"
        f"measured_TP={measured['input_tp']}:measured_LRA={measured['input_lra']}:"
        f"measured_thresh={measured['input_thresh']}:"
        f"offset={measured.get('target_offset', '0')}:linear=true,"
        f"alimiter=limit={ALIMITER_CEILING_LINEAR}:level=disabled,aresample=48000"
    )
    corrective = f"{prefilter},{loudnorm}" if prefilter else loudnorm

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
        "voice_provider": str(voice_provider or ""),
        "target_integrated_lufs": TARGET_INTEGRATED_LUFS,
        "target_true_peak_dbtp": TARGET_TRUE_PEAK_DBTP,
        "target_loudness_range": TARGET_LOUDNESS_RANGE,
        "alimiter_ceiling_linear": ALIMITER_CEILING_LINEAR,
        "corrective_profile": profile,
        "corrective_filter": prefilter,
        "tempo_or_pitch_change": False,
        "measured_input_integrated_lufs": float(measured["input_i"]),
        "measured_input_true_peak_dbtp": float(measured["input_tp"]),
    }

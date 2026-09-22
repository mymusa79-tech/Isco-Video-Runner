from __future__ import annotations

import math
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

from .media import probe_duration

SCHEMA_VERSION = 1
MUSIC_TARGET_DELTA_DB = -22.5
MUSIC_MIN_DELTA_DB = -25.0
MUSIC_MAX_DELTA_DB = -20.0
SFX_TARGET_DELTA_DB = -24.0
SFX_MIN_DELTA_DB = -30.0
SFX_MAX_DELTA_DB = -18.0
HOOK_SFX_OFFSET_SECONDS = 0.18
MAX_SFX_ACCENTS = 2

_SECRET_ENV_NAMES = {
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "OPENROUTER_API_KEY",
    "MISTRAL_API_KEY",
    "PEXELS_API_KEY",
    "PIXABAY_API_KEY",
    "CLOUDFLARE_API_TOKEN",
    "AZURE_SPEECH_KEY",
    "YOUTUBE_API_KEY",
    "YOUTUBE_CLIENT_SECRET",
    "YOUTUBE_REFRESH_TOKEN",
    "TELEGRAM_BOT_TOKEN",
}

_MEAN_VOLUME_RE = re.compile(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", re.I)


class ShortAudioPolishError(RuntimeError):
    pass


def _secret_free_subprocess_env() -> dict[str, str]:
    child_env = os.environ.copy()
    for name in list(child_env):
        upper = name.upper()
        if name in _SECRET_ENV_NAMES or upper.endswith("_API_KEY") or upper.endswith("_TOKEN"):
            child_env.pop(name, None)
    return child_env


def _run(command: list[str], *, timeout: int = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_secret_free_subprocess_env(),
    )


def _measure_mean_volume_db(path: Path, *, audio_filter: str | None = None) -> float:
    command = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-i",
        str(Path(path).resolve()),
    ]
    filters = []
    if audio_filter:
        filters.append(audio_filter)
    filters.append("volumedetect")
    command.extend(["-af", ",".join(filters), "-f", "null", "-"])
    proc = _run(command, timeout=120)
    match = _MEAN_VOLUME_RE.search(proc.stderr)
    if not match:
        raise ShortAudioPolishError(f"mean_volume_unavailable file={Path(path).name}")
    value = float(match.group(1))
    if not math.isfinite(value):
        raise ShortAudioPolishError(f"mean_volume_invalid file={Path(path).name}")
    return value


def _gain_for_relative_target(source_mean_db: float, narration_mean_db: float, delta_db: float) -> float:
    return (narration_mean_db + delta_db) - source_mean_db


def _assert_delta(name: str, delta_db: float, low: float, high: float) -> None:
    if not low <= delta_db <= high:
        raise ShortAudioPolishError(
            f"{name}_relative_level_out_of_range actual={delta_db:.2f} allowed={low:.1f}..{high:.1f}"
        )


def _generate_music(path: Path, duration: float) -> Path:
    if duration <= 0:
        raise ShortAudioPolishError("music_duration_invalid")
    fade_out_start = max(0.0, duration - 1.25)
    # One restrained, percussion-free ambient pad. This is intentionally the simple
    # V1 requested by the channel: no internal beat-sync or mood switching yet.
    expr = (
        "0.10*sin(2*PI*130.81*t)"
        "+0.052*sin(2*PI*196.00*t)"
        "+0.032*sin(2*PI*261.63*t)"
    )
    _run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"aevalsrc={expr}:s=48000:d={duration:.3f}",
            "-af",
            (
                "lowpass=f=1800,"
                "afade=t=in:st=0:d=1.0,"
                f"afade=t=out:st={fade_out_start:.3f}:d={min(1.25, duration):.3f}"
            ),
            "-c:a",
            "pcm_s16le",
            str(path.resolve()),
        ]
    )
    return path


def _generate_hook_sfx(path: Path) -> Path:
    # Soft chime: no click, no transient spike, long enough fade to remain subtle.
    _run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=659.25:sample_rate=48000:duration=0.72",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=987.77:sample_rate=48000:duration=0.72",
            "-filter_complex",
            (
                "[0:a]volume=0.52,afade=t=in:st=0:d=0.08,afade=t=out:st=0.16:d=0.56[a0];"
                "[1:a]volume=0.22,afade=t=in:st=0:d=0.08,afade=t=out:st=0.12:d=0.60[a1];"
                "[a0][a1]amix=inputs=2:normalize=0,lowpass=f=2200[out]"
            ),
            "-map",
            "[out]",
            "-c:a",
            "pcm_s16le",
            str(path.resolve()),
        ]
    )
    return path


def _generate_payoff_sfx(path: Path) -> Path:
    # Warmer terminal tone than the hook, still without percussion or a sharp attack.
    _run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=523.25:sample_rate=48000:duration=0.92",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=783.99:sample_rate=48000:duration=0.92",
            "-filter_complex",
            (
                "[0:a]volume=0.50,afade=t=in:st=0:d=0.12,afade=t=out:st=0.20:d=0.72[a0];"
                "[1:a]volume=0.16,afade=t=in:st=0:d=0.12,afade=t=out:st=0.18:d=0.74[a1];"
                "[a0][a1]amix=inputs=2:normalize=0,lowpass=f=1900[out]"
            ),
            "-map",
            "[out]",
            "-c:a",
            "pcm_s16le",
            str(path.resolve()),
        ]
    )
    return path


def _payoff_start_seconds(events: Sequence[Mapping[str, object]] | None) -> float | None:
    if not events:
        return None
    for item in events:
        if not isinstance(item, Mapping):
            continue
        if str(item.get("role") or "").strip().lower() != "payoff":
            continue
        try:
            value = float(item.get("start"))
        except (TypeError, ValueError):
            return None
        if math.isfinite(value) and value >= 0:
            return value
    return None


def build_procedural_stems(
    *,
    output_dir: Path,
    narration_path: Path,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    duration = probe_duration(Path(narration_path))
    if duration <= 0:
        raise ShortAudioPolishError("narration_duration_invalid")

    music = output_dir / "short-music-bed.wav"
    hook = output_dir / "short-sfx-hook.wav"
    payoff = output_dir / "short-sfx-payoff.wav"
    _generate_music(music, duration)
    _generate_hook_sfx(hook)
    _generate_payoff_sfx(payoff)

    narration_mean = _measure_mean_volume_db(Path(narration_path))
    music_source_mean = _measure_mean_volume_db(music)
    hook_source_mean = _measure_mean_volume_db(hook)
    payoff_source_mean = _measure_mean_volume_db(payoff)

    music_gain = _gain_for_relative_target(
        music_source_mean, narration_mean, MUSIC_TARGET_DELTA_DB
    )
    hook_gain = _gain_for_relative_target(
        hook_source_mean, narration_mean, SFX_TARGET_DELTA_DB
    )
    payoff_gain = _gain_for_relative_target(
        payoff_source_mean, narration_mean, SFX_TARGET_DELTA_DB
    )

    music_actual = _measure_mean_volume_db(music, audio_filter=f"volume={music_gain:.3f}dB")
    hook_actual = _measure_mean_volume_db(hook, audio_filter=f"volume={hook_gain:.3f}dB")
    payoff_actual = _measure_mean_volume_db(payoff, audio_filter=f"volume={payoff_gain:.3f}dB")

    music_delta = music_actual - narration_mean
    hook_delta = hook_actual - narration_mean
    payoff_delta = payoff_actual - narration_mean
    _assert_delta("music", music_delta, MUSIC_MIN_DELTA_DB, MUSIC_MAX_DELTA_DB)
    _assert_delta("hook_sfx", hook_delta, SFX_MIN_DELTA_DB, SFX_MAX_DELTA_DB)
    _assert_delta("payoff_sfx", payoff_delta, SFX_MIN_DELTA_DB, SFX_MAX_DELTA_DB)

    return {
        "duration_seconds": duration,
        "narration_mean_db": narration_mean,
        "music": {
            "path": music,
            "gain_db": music_gain,
            "measured_mean_db": music_actual,
            "relative_to_narration_db": music_delta,
        },
        "hook_sfx": {
            "path": hook,
            "gain_db": hook_gain,
            "measured_mean_db": hook_actual,
            "relative_to_narration_db": hook_delta,
        },
        "payoff_sfx": {
            "path": payoff,
            "gain_db": payoff_gain,
            "measured_mean_db": payoff_actual,
            "relative_to_narration_db": payoff_delta,
        },
    }


def apply_short_audio_polish(
    *,
    output_dir: Path,
    final_path: Path,
    narration_path: Path,
    timed_text_events: Sequence[Mapping[str, object]] | None = None,
) -> dict[str, Any]:
    """Add optional low-level calm music and at most two gentle accents.

    This runs after narration mastering and never modifies narration-mastered.wav.
    Any asset generation, level validation, or mix failure is fail-safe: the already
    valid video remains untouched.
    """
    output_dir = Path(output_dir)
    final_path = Path(final_path)
    work = output_dir / "short-audio-polish"
    work.mkdir(parents=True, exist_ok=True)
    mixed = output_dir / ".final-short-audio-polish.mp4"

    base_report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "source": "clean-v2-short-calm-audio-polish",
        "status": "skipped_fail_safe",
        "required": False,
        "provider_calls_added": 0,
        "narration_mastering_untouched": True,
        "music_profile": "single_calm_reflective_pad_v1",
        "dynamic_mood_inside_short": False,
        "future_improvement": "section-aware warmer payoff without increasing narration competition",
        "music_target_delta_db": MUSIC_TARGET_DELTA_DB,
        "music_allowed_delta_db": [MUSIC_MIN_DELTA_DB, MUSIC_MAX_DELTA_DB],
        "sfx_target_delta_db": SFX_TARGET_DELTA_DB,
        "sfx_allowed_delta_db": [SFX_MIN_DELTA_DB, SFX_MAX_DELTA_DB],
        "max_sfx_accents": MAX_SFX_ACCENTS,
        "sharp_attacks": False,
        "percussion": False,
        "provenance": {
            "music": "local_ffmpeg_procedural_no_third_party_asset",
            "sfx": "local_ffmpeg_procedural_no_third_party_asset",
            "free_libraries_researched": ["YouTube Audio Library", "Pixabay Music"],
        },
    }

    try:
        stems = build_procedural_stems(
            output_dir=work,
            narration_path=Path(narration_path),
        )
        payoff_start = _payoff_start_seconds(timed_text_events)
        if payoff_start is None:
            payoff_start = max(0.0, float(stems["duration_seconds"]) - 2.2)

        hook_delay_ms = int(round(HOOK_SFX_OFFSET_SECONDS * 1000))
        payoff_delay_ms = int(round(payoff_start * 1000))
        music = stems["music"]
        hook = stems["hook_sfx"]
        payoff = stems["payoff_sfx"]

        filter_complex = (
            f"[1:a]volume={float(music['gain_db']):.3f}dB[music];"
            f"[2:a]volume={float(hook['gain_db']):.3f}dB,"
            f"adelay={hook_delay_ms}|{hook_delay_ms}[hook];"
            f"[3:a]volume={float(payoff['gain_db']):.3f}dB,"
            f"adelay={payoff_delay_ms}|{payoff_delay_ms}[payoff];"
            "[0:a][music][hook][payoff]"
            "amix=inputs=4:duration=first:normalize=0,"
            "alimiter=limit=0.95:level=disabled[aout]"
        )
        _run(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(final_path.resolve()),
                "-i",
                str(Path(music["path"]).resolve()),
                "-i",
                str(Path(hook["path"]).resolve()),
                "-i",
                str(Path(payoff["path"]).resolve()),
                "-filter_complex",
                filter_complex,
                "-map",
                "0:v:0",
                "-map",
                "[aout]",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-shortest",
                str(mixed.resolve()),
            ],
            timeout=180,
        )
        if not mixed.is_file() or mixed.stat().st_size <= 0:
            raise ShortAudioPolishError("mixed_video_missing_or_empty")
        os.replace(mixed, final_path)

        return {
            **base_report,
            "status": "pass",
            "music_applied": True,
            "hook_sfx_applied": True,
            "payoff_sfx_applied": True,
            "sfx_accent_count": 2,
            "hook_sfx_offset_seconds": HOOK_SFX_OFFSET_SECONDS,
            "payoff_sfx_offset_seconds": payoff_start,
            "measured_levels": {
                "narration_mean_db": stems["narration_mean_db"],
                "music_mean_db": music["measured_mean_db"],
                "music_relative_to_narration_db": music["relative_to_narration_db"],
                "hook_sfx_mean_db": hook["measured_mean_db"],
                "hook_sfx_relative_to_narration_db": hook["relative_to_narration_db"],
                "payoff_sfx_mean_db": payoff["measured_mean_db"],
                "payoff_sfx_relative_to_narration_db": payoff["relative_to_narration_db"],
            },
        }
    except Exception as exc:
        mixed.unlink(missing_ok=True)
        return {
            **base_report,
            "music_applied": False,
            "hook_sfx_applied": False,
            "payoff_sfx_applied": False,
            "sfx_accent_count": 0,
            "reason": f"{type(exc).__name__}: {exc}",
        }

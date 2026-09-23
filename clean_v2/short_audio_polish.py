from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any, Mapping

MUSIC_TARGET_REL_DB = -18.5
MUSIC_MIN_REL_DB = -21.0
MUSIC_MAX_REL_DB = -16.5
SFX_TARGET_REL_DB = -16.5
PAYOFF_SFX_TARGET_REL_DB = -17.5
SFX_MIN_REL_DB = -22.0
SFX_MAX_REL_DB = -14.5
LEVEL_TOLERANCE_DB = 1.0
HOOK_SFX_DELAY_SECONDS = 0.06

_MEAN_VOLUME_RE = re.compile(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", re.I)

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


def _secret_free_env() -> dict[str, str]:
    env = os.environ.copy()
    for name in list(env):
        upper = name.upper()
        if name in _SECRET_ENV_NAMES or upper.endswith("_API_KEY") or upper.endswith("_TOKEN"):
            env.pop(name, None)
    return env


def _run(command: list[str], *, timeout: int = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_secret_free_env(),
    )


def _measure_mean_db(path: Path) -> float:
    proc = _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-af",
            "volumedetect",
            "-f",
            "null",
            "-",
        ]
    )
    match = _MEAN_VOLUME_RE.search(proc.stderr or "")
    if not match:
        raise RuntimeError(f"short_audio_level_measurement_missing path={path.name}")
    return float(match.group(1))


def _apply_gain(src: Path, dest: Path, gain_db: float) -> Path:
    _run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(src),
            "-af",
            f"volume={gain_db:.3f}dB",
            "-c:a",
            "pcm_s16le",
            str(dest),
        ]
    )
    if not dest.is_file() or dest.stat().st_size <= 0:
        raise RuntimeError(f"short_audio_gain_output_missing path={dest.name}")
    return dest


def _normalize_relative(
    *,
    src: Path,
    dest: Path,
    narration_mean_db: float,
    target_relative_db: float,
    minimum_relative_db: float,
    maximum_relative_db: float,
) -> dict[str, float]:
    before = _measure_mean_db(src)
    target_mean = narration_mean_db + target_relative_db
    gain = target_mean - before
    _apply_gain(src, dest, gain)
    after = _measure_mean_db(dest)
    relative = after - narration_mean_db
    if not minimum_relative_db <= relative <= maximum_relative_db:
        raise RuntimeError(
            "short_audio_relative_level_out_of_range "
            f"relative_db={relative:.3f} allowed={minimum_relative_db:.1f}..{maximum_relative_db:.1f}"
        )
    return {
        "source_mean_db": before,
        "gain_db": gain,
        "mean_db": after,
        "relative_to_narration_db": relative,
    }


def _generate_raw_music(dest: Path, duration: float) -> Path:
    """Create a non-tonal evolving ambient bed; avoids the old stationary hum."""
    fade_out_start = max(0.0, duration - 1.0)
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
            f"anoisesrc=color=pink:amplitude=0.035:sample_rate=48000:duration={duration:.3f}",
            "-f",
            "lavfi",
            "-i",
            f"anoisesrc=color=brown:amplitude=0.018:sample_rate=48000:duration={duration:.3f}",
            "-filter_complex",
            (
                "[0:a]highpass=f=180,lowpass=f=3600,tremolo=f=0.10:d=0.20[a0];"
                "[1:a]highpass=f=70,lowpass=f=700,tremolo=f=0.10:d=0.16[a1];"
                "[a0][a1]amix=inputs=2:normalize=0,"
                "afade=t=in:st=0:d=0.7,"
                f"afade=t=out:st={fade_out_start:.3f}:d=1.0[a]"
            ),
            "-map",
            "[a]",
            "-c:a",
            "pcm_s16le",
            str(dest),
        ]
    )
    return dest


def _prepare_local_music_bed(src: Path, dest: Path, duration: float) -> Path:
    """Loop one optional owned/local bed; no download and no provider call."""
    fade_out_start = max(0.0, duration - 1.0)
    _run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-stream_loop", "-1", "-i", str(src),
            "-af",
            f"atrim=duration={duration:.3f},asetpts=PTS-STARTPTS,"
            "afade=t=in:st=0:d=0.7,"
            f"afade=t=out:st={fade_out_start:.3f}:d=1.0",
            "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", str(dest),
        ]
    )
    return dest


def _generate_raw_sfx(dest: Path, *, frequency: float) -> Path:
    """Generate one of two restrained original-owned accents.

    The lower-frequency hook request becomes the Engine's proven soft air-whoosh
    family rather than a bare sine beep; the payoff keeps a warm tonal chime.
    The public helper signature stays unchanged for regression compatibility.
    """
    if frequency < 600.0:
        source = "anoisesrc=color=pink:amplitude=0.06:sample_rate=48000:duration=0.52"
        audio_filter = (
            "highpass=f=900,lowpass=f=6000,"
            "afade=t=in:st=0:d=0.12,afade=t=out:st=0.28:d=0.24"
        )
    else:
        source = f"sine=frequency={frequency:.2f}:sample_rate=48000:duration=0.48"
        audio_filter = "afade=t=in:st=0:d=0.025,afade=t=out:st=0.30:d=0.18"
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
            source,
            "-af",
            audio_filter,
            "-c:a",
            "pcm_s16le",
            str(dest),
        ]
    )
    return dest


def _payoff_start_seconds(timed_text_report: Mapping[str, Any] | None, duration: float) -> float:
    if isinstance(timed_text_report, Mapping):
        events = timed_text_report.get("events")
        if isinstance(events, list) and events:
            last = events[-1]
            if isinstance(last, Mapping):
                try:
                    value = float(last.get("start"))
                except (TypeError, ValueError):
                    value = -1.0
                if 0.0 <= value < duration:
                    return value
    return max(0.0, duration * 0.72)


def _mix_into_video(
    *,
    final_path: Path,
    output_path: Path,
    music_path: Path | None,
    hook_sfx_path: Path | None,
    payoff_sfx_path: Path | None,
    payoff_start_seconds: float,
) -> None:
    command = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(final_path),
    ]
    stems: list[tuple[str, Path, float]] = []
    if music_path is not None:
        stems.append(("music", music_path, 0.0))
    if hook_sfx_path is not None:
        stems.append(("hook", hook_sfx_path, HOOK_SFX_DELAY_SECONDS))
    if payoff_sfx_path is not None:
        stems.append(("payoff", payoff_sfx_path, payoff_start_seconds))

    for _, path, _ in stems:
        command.extend(["-i", str(path)])

    filters: list[str] = []
    labels = ["[0:a]"]
    for input_index, (name, _, delay_seconds) in enumerate(stems, start=1):
        label = f"[{name}]"
        if delay_seconds > 0:
            delay_ms = int(round(delay_seconds * 1000))
            filters.append(f"[{input_index}:a]adelay={delay_ms}|{delay_ms}{label}")
        else:
            filters.append(f"[{input_index}:a]anull{label}")
        labels.append(label)
    filters.append(
        "".join(labels)
        + f"amix=inputs={len(labels)}:normalize=0:duration=first:dropout_transition=0,"
        "alimiter=limit=0.95:level=disabled[mix]"
    )

    command.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "0:v:0",
            "-map",
            "[mix]",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
            str(output_path),
        ]
    )
    _run(command)
    if not output_path.is_file() or output_path.stat().st_size <= 0:
        raise RuntimeError("short_audio_mix_output_missing_or_empty")


def apply_short_audio_polish(
    *,
    output_dir: Path,
    final_path: Path,
    narration_path: Path,
    timed_text_report: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Add a fail-safe, very low-level calm music bed and at most two gentle accents.

    Narration has already passed loudness mastering before this function runs. This
    layer never re-normalizes or re-masters narration; it measures the mastered
    narration only to place optional stems safely underneath it.
    """
    output_dir = Path(output_dir)
    final_path = Path(final_path)
    narration_path = Path(narration_path)
    temp_dir = output_dir / ".short-audio-polish"
    temp_dir.mkdir(parents=True, exist_ok=True)

    from .media import probe_duration

    duration = probe_duration(narration_path)
    narration_mean_db = _measure_mean_db(narration_path)
    component_reports: dict[str, dict[str, Any]] = {}
    ready: dict[str, Path] = {}

    specs = (
        ("music", MUSIC_TARGET_REL_DB, MUSIC_MIN_REL_DB, MUSIC_MAX_REL_DB),
        ("hook_sfx", SFX_TARGET_REL_DB, SFX_MIN_REL_DB, SFX_MAX_REL_DB),
        ("payoff_sfx", PAYOFF_SFX_TARGET_REL_DB, SFX_MIN_REL_DB, SFX_MAX_REL_DB),
    )

    for name, target, minimum, maximum in specs:
        raw = temp_dir / f"{name}-raw.wav"
        adjusted = temp_dir / f"{name}.wav"
        try:
            if name == "music":
                local_bed = str(os.environ.get("CLEAN_V2_SHORT_MUSIC_BED") or "").strip()
                local_path = Path(local_bed).expanduser() if local_bed else None
                if local_path is not None and local_path.is_file() and local_path.stat().st_size > 1024:
                    _prepare_local_music_bed(local_path, raw, duration)
                else:
                    _generate_raw_music(raw, duration)
            elif name == "hook_sfx":
                _generate_raw_sfx(raw, frequency=523.25)
            else:
                _generate_raw_sfx(raw, frequency=659.25)
            level = _normalize_relative(
                src=raw,
                dest=adjusted,
                narration_mean_db=narration_mean_db,
                target_relative_db=target,
                minimum_relative_db=minimum,
                maximum_relative_db=maximum,
            )
            component_reports[name] = {"status": "ready", **level}
            ready[name] = adjusted
        except Exception as exc:
            component_reports[name] = {
                "status": "skipped_fail_safe",
                "reason": type(exc).__name__,
            }

    mix_output = temp_dir / "final-audio-polished.mp4"
    payoff_start = _payoff_start_seconds(timed_text_report, duration)
    try:
        if ready:
            _mix_into_video(
                final_path=final_path,
                output_path=mix_output,
                music_path=ready.get("music"),
                hook_sfx_path=ready.get("hook_sfx"),
                payoff_sfx_path=ready.get("payoff_sfx"),
                payoff_start_seconds=payoff_start,
            )
            os.replace(mix_output, final_path)
            status = "pass"
        else:
            status = "skipped_fail_safe"
    except Exception as exc:
        mix_output.unlink(missing_ok=True)
        status = "skipped_fail_safe"
        component_reports["mix"] = {
            "status": "skipped_fail_safe",
            "reason": type(exc).__name__,
        }
    finally:
        for path in temp_dir.glob("*"):
            path.unlink(missing_ok=True)
        try:
            temp_dir.rmdir()
        except OSError:
            pass

    return {
        "schema_version": 1,
        "source": "clean-v2-short-audio-polish-v2",
        "status": status,
        "asset_origin": "optional_local_owned_bed_or_non_tonal_procedural_ffmpeg",
        "external_download_required": False,
        "narration_mastering_untouched": True,
        "narration_mean_db": narration_mean_db,
        "music_target_relative_db": MUSIC_TARGET_REL_DB,
        "music_allowed_relative_db": [MUSIC_MIN_REL_DB, MUSIC_MAX_REL_DB],
        "sfx_allowed_relative_db": [SFX_MIN_REL_DB, SFX_MAX_REL_DB],
        "hook_sfx_target_relative_db": SFX_TARGET_REL_DB,
        "payoff_sfx_target_relative_db": PAYOFF_SFX_TARGET_REL_DB,
        "sfx_profile": "engine-procedural-air-whoosh-plus-warm-chime",
        "percussion": False,
        "sharp_attacks": False,
        "hook_sfx_max_count": 1,
        "payoff_sfx_max_count": 1,
        "payoff_start_seconds": payoff_start,
        "components": component_reports,
        "provider_calls_added": 0,
        "fail_safe": True,
        "internal_music_tone_change": "stationary_sine_hum_removed",
    }

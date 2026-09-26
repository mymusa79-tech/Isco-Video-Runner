from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Mapping

from .music_library import select_music_track

MUSIC_TARGET_REL_DB = -22.0
MUSIC_MIN_REL_DB = -25.0
MUSIC_MAX_REL_DB = -20.0
LEVEL_TOLERANCE_DB = 1.0

# Compatibility constants. Generated SFX are intentionally disabled.
SFX_TARGET_REL_DB = -120.0
SFX_MIN_REL_DB = -120.0
SFX_MAX_REL_DB = -120.0

_MEAN_VOLUME_RE = re.compile(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", re.I)
_SECRET_ENV_NAMES = {
    "GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY", "MISTRAL_API_KEY",
    "PEXELS_API_KEY", "PIXABAY_API_KEY", "CLOUDFLARE_API_TOKEN",
    "AZURE_SPEECH_KEY", "YOUTUBE_API_KEY", "YOUTUBE_CLIENT_SECRET",
    "YOUTUBE_REFRESH_TOKEN", "TELEGRAM_BOT_TOKEN",
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
        command, check=True, capture_output=True, text=True, timeout=timeout,
        env=_secret_free_env(),
    )


def _measure_mean_db(path: Path) -> float:
    proc = _run([
        "ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
        "-af", "volumedetect", "-f", "null", "-",
    ])
    match = _MEAN_VOLUME_RE.search(proc.stderr or "")
    if not match:
        raise RuntimeError(f"short_audio_level_measurement_missing path={path.name}")
    return float(match.group(1))


def _apply_gain(src: Path, dest: Path, gain_db: float) -> Path:
    _run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src), "-af", f"volume={gain_db:.3f}dB",
        "-c:a", "pcm_s16le", str(dest),
    ])
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


def _generate_raw_music(_dest: Path, _duration: float) -> Path:
    raise RuntimeError("procedural_noise_music_disabled_by_director_layout_v1")


def _generate_raw_sfx(_dest: Path, *, frequency: float) -> Path:
    del frequency
    raise RuntimeError("generated_sfx_disabled_by_director_layout_v1")


def _topic_window(output_dir: Path) -> tuple[float, float]:
    path = Path(output_dir) / "timeline-first.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("topic_music_timeline_missing") from exc
    for row in payload.get("identity_events") or []:
        if isinstance(row, Mapping) and str(row.get("kind") or "") == "topic":
            start = float(row.get("start") or 0.0)
            end = float(row.get("end") or 0.0)
            if start >= 0 and end > start:
                return start, end
    raise RuntimeError("topic_music_window_missing")


def _prepare_local_music_bed(src: Path, dest: Path, duration: float) -> Path:
    fade = min(0.65, max(0.12, duration / 6.0))
    fade_out_start = max(0.0, duration - fade)
    _run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-stream_loop", "-1", "-i", str(src),
        "-af",
        f"atrim=duration={duration:.3f},asetpts=PTS-STARTPTS,"
        f"afade=t=in:st=0:d={fade:.3f},"
        f"afade=t=out:st={fade_out_start:.3f}:d={fade:.3f}",
        "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", str(dest),
    ])
    if not dest.is_file() or dest.stat().st_size <= 0:
        raise RuntimeError("topic_music_preparation_failed")
    return dest


def _mix_music_into_video(
    *,
    final_path: Path,
    output_path: Path,
    music_path: Path,
    topic_start_seconds: float,
) -> None:
    delay_ms = int(round(topic_start_seconds * 1000))
    _run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(final_path), "-i", str(music_path),
        "-filter_complex",
        (
            f"[1:a]adelay={delay_ms}|{delay_ms}[music];"
            "[0:a][music]amix=inputs=2:normalize=0:duration=first:dropout_transition=0,"
            "alimiter=limit=0.95:level=disabled[mix]"
        ),
        "-map", "0:v:0", "-map", "[mix]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest",
        str(output_path),
    ])
    if not output_path.is_file() or output_path.stat().st_size <= 0:
        raise RuntimeError("topic_audio_mix_output_missing_or_empty")


def apply_topic_audio_polish(
    *,
    output_dir: Path,
    final_path: Path,
    narration_path: Path,
    script: Mapping[str, Any] | None = None,
    fmt: str,
) -> dict[str, Any]:
    """Mix one verified CC0 music bed only inside the shared measured topic window."""
    if fmt not in {"short", "film", "podcast"}:
        raise ValueError(f"topic audio polish unsupported format: {fmt}")
    output_dir = Path(output_dir)
    final_path = Path(final_path)
    narration_path = Path(narration_path)
    temp_dir = output_dir / ".topic-audio-polish"
    temp_dir.mkdir(parents=True, exist_ok=True)
    narration_mean_db = _measure_mean_db(narration_path)

    diagnosis = {
        "status": "confirmed" if fmt == "short" else "not_applicable_prior_short_only",
        "root_cause": "post_master_procedural_noise_layer" if fmt == "short" else None,
        "prior_music_source": "ffmpeg pink+brown anoisesrc" if fmt == "short" else None,
        "prior_hook_sfx_source": "ffmpeg pink anoisesrc branch at 523.25Hz" if fmt == "short" else None,
        "mastering_fault": False,
        "fix": "verified local CC0 topic-only music; generated noise/sfx disabled",
    }
    library_report: dict[str, Any] = {}
    component: dict[str, Any] = {}
    temp_mix = temp_dir / "final-audio-polished.mp4"
    topic_start: float | None = None
    topic_end: float | None = None
    try:
        topic_start, topic_end = _topic_window(output_dir)
        topic_duration = topic_end - topic_start
        track_path, library_report = select_music_track(
            script,
            fmt=fmt,
            allow_download=True,
        )
        if track_path is None:
            raise RuntimeError("verified_music_track_unavailable")

        raw_bed = temp_dir / "music-window-raw.wav"
        adjusted = temp_dir / "music-window.wav"
        _prepare_local_music_bed(track_path, raw_bed, topic_duration)
        level = _normalize_relative(
            src=raw_bed,
            dest=adjusted,
            narration_mean_db=narration_mean_db,
            target_relative_db=MUSIC_TARGET_REL_DB,
            minimum_relative_db=MUSIC_MIN_REL_DB,
            maximum_relative_db=MUSIC_MAX_REL_DB,
        )
        component = {
            "status": "ready",
            "track_id": library_report.get("selected_id"),
            "track_title": library_report.get("selected_title"),
            **level,
        }
        _mix_music_into_video(
            final_path=final_path,
            output_path=temp_mix,
            music_path=adjusted,
            topic_start_seconds=topic_start,
        )
        os.replace(temp_mix, final_path)
        status = "pass"
        reason = None
    except Exception as exc:
        temp_mix.unlink(missing_ok=True)
        status = "skipped_fail_safe"
        reason = f"{type(exc).__name__}:{str(exc)[:120]}"
    finally:
        for path in temp_dir.glob("*"):
            path.unlink(missing_ok=True)
        try:
            temp_dir.rmdir()
        except OSError:
            pass

    return {
        "schema_version": 5,
        "source": "clean-v2-topic-audio-polish-v2-music-studio-lite",
        "format": fmt,
        "status": status,
        "reason": reason,
        "asset_origin": "verified_cc0_freepd_local_cache",
        "external_download_required": bool(library_report.get("allow_download", False)),
        "narration_mastering_untouched": True,
        "narration_mean_db": narration_mean_db,
        "music_target_relative_db": MUSIC_TARGET_REL_DB,
        "music_allowed_relative_db": [MUSIC_MIN_REL_DB, MUSIC_MAX_REL_DB],
        "music_window": (
            {"start": topic_start, "end": topic_end}
            if topic_start is not None and topic_end is not None
            else None
        ),
        "music_before_topic": False,
        "music_during_outro": False,
        "music_during_final_silence": False,
        "generated_music": False,
        "generated_sfx": False,
        "components": {"music": component},
        "library": library_report,
        "noise_diagnosis": diagnosis,
        "provider_calls_added": 0,
        "fail_safe": True,
    }



def apply_short_audio_polish(
    *,
    output_dir: Path,
    final_path: Path,
    narration_path: Path,
    timed_text_report: Mapping[str, Any] | None = None,
    script: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Backward-compatible Short wrapper over the shared topic-only mixer."""
    del timed_text_report
    return apply_topic_audio_polish(
        output_dir=output_dir,
        final_path=final_path,
        narration_path=narration_path,
        script=script,
        fmt="short",
    )

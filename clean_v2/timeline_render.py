from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Mapping

from .identity_sequence import identity_asset_paths


def _event(timeline: Mapping[str, Any], kind: str) -> Mapping[str, Any]:
    rows = timeline.get("identity_events")
    matches = [
        row for row in rows or []
        if isinstance(row, Mapping) and str(row.get("kind") or "") == kind
    ]
    if len(matches) != 1:
        raise RuntimeError(f"timeline identity event invalid: {kind}")
    return matches[0]



def render_identity_composition(
    source: Path,
    destination: Path,
    *,
    fmt: str,
    timeline: Mapping[str, Any],
) -> None:
    """Composite fixed identity inside the narration-owned duration."""
    assets = identity_asset_paths(fmt, runtime_dir=destination.parent / ".identity-assets")
    for name in ("intro", "prayer", "outro"):
        asset = assets[name]
        if not asset.is_file() or asset.stat().st_size <= 1024:
            raise RuntimeError(f"identity asset missing: {name}")

    voice_seconds = float(timeline.get("voice_seconds_measured") or 0.0)
    if voice_seconds <= 0:
        raise RuntimeError("timeline voice duration missing")

    width, height = ((1080, 1920) if fmt == "short" else (1920, 1080))
    def bounds(kind: str) -> tuple[float, float]:
        row = _event(timeline, kind)
        start = float(row.get("start") or 0.0)
        end = float(row.get("end") or 0.0)
        if start < 0 or end <= start or end > voice_seconds + 0.08:
            raise RuntimeError(f"timeline identity bounds invalid: {kind}")
        return start, min(end, voice_seconds)

    intro_start, intro_end = bounds("intro")
    prayer_start, prayer_end = bounds("prayer")
    outro_start, outro_end = bounds("outro")
    final_silence_start, final_silence_end = bounds("final_silence")

    intro_duration = max(0.001, intro_end - intro_start)
    prayer_duration = max(0.001, prayer_end - prayer_start)
    outro_duration = max(0.001, outro_end - outro_start)

    if fmt == "podcast":
        # V8 assets are the approved Outside the Text identity. Their own clean
        # signature SFX are mixed only inside the intro/outro windows; prayer and
        # the A/B conversation remain free of identity SFX.
        intro_delay_ms = int(round(intro_start * 1000))
        outro_delay_ms = int(round(outro_start * 1000))
        filters = (
            f"[1:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1,fps=30,format=rgba,"
            f"tpad=stop_mode=clone:stop_duration={intro_duration:.3f},"
            f"trim=duration={intro_duration:.3f},"
            f"setpts=PTS-STARTPTS+{intro_start:.3f}/TB[intro];"
            f"[2:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1,fps=30,format=rgba,"
            f"trim=duration={prayer_duration:.3f},"
            f"setpts=PTS-STARTPTS+{prayer_start:.3f}/TB[prayer];"
            f"[3:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1,fps=30,format=rgba,"
            f"tpad=stop_mode=clone:stop_duration={outro_duration:.3f},"
            f"trim=duration={outro_duration:.3f},"
            f"setpts=PTS-STARTPTS+{outro_start:.3f}/TB[outro];"
            f"[0:v][intro]overlay=0:0:enable='between(t,{intro_start:.3f},{intro_end:.3f})'[v1];"
            f"[v1][prayer]overlay=0:0:"
            f"enable='between(t,{prayer_start:.3f},{prayer_end:.3f})'[v2];"
            f"[v2][outro]overlay=0:0:"
            f"enable='between(t,{outro_start:.3f},{final_silence_end:.3f})'[vout];"
            f"[1:a]atrim=duration={intro_duration:.3f},asetpts=PTS-STARTPTS,"
            f"adelay={intro_delay_ms}:all=1[aintro];"
            f"[3:a]atrim=duration={outro_duration:.3f},asetpts=PTS-STARTPTS,"
            f"adelay={outro_delay_ms}:all=1[aoutro];"
            f"[0:a][aintro][aoutro]amix=inputs=3:normalize=0:duration=first:dropout_transition=0,"
            f"alimiter=limit=0.84:level=disabled[aout]"
        )
        audio_map = "[aout]"
        audio_codec = ["-c:a", "aac", "-b:a", "192k"]
    else:
        freeze_duration = max(0.0, final_silence_end - final_silence_start)
        filters = (
            f"[1:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1,fps=30,format=rgba,"
            f"tpad=stop_mode=clone:stop_duration={intro_duration:.3f},"
            f"trim=duration={intro_duration:.3f},"
            f"setpts=PTS-STARTPTS+{intro_start:.3f}/TB[intro];"
            f"[2:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1,fps=30,format=rgba,"
            f"trim=duration={prayer_duration:.3f},"
            f"setpts=PTS-STARTPTS+{prayer_start:.3f}/TB[prayer];"
            f"[3:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1,fps=30,format=rgba,"
            f"tpad=stop_mode=clone:stop_duration={outro_duration + freeze_duration:.3f},"
            f"trim=duration={outro_duration + freeze_duration:.3f},"
            f"setpts=PTS-STARTPTS+{outro_start:.3f}/TB[outro];"
            f"[0:v][intro]overlay=0:0:enable='between(t,{intro_start:.3f},{intro_end:.3f})'[v1];"
            f"[v1][prayer]overlay=0:0:"
            f"enable='between(t,{prayer_start:.3f},{prayer_end:.3f})'[v2];"
            f"[v2][outro]overlay=0:0:"
            f"enable='between(t,{outro_start:.3f},{final_silence_end:.3f})'[vout]"
        )
        audio_map = "0:a:0"
        audio_codec = ["-c:a", "copy"]

    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(source),
            "-i", str(assets["intro"]),
            "-loop", "1", "-framerate", "30", "-i", str(assets["prayer"]),
            "-i", str(assets["outro"]),
            "-filter_complex", filters,
            "-map", "[vout]", "-map", audio_map,
            "-t", f"{voice_seconds:.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
            "-pix_fmt", "yuv420p", *audio_codec,
            "-movflags", "+faststart", str(destination),
        ],
        check=True,
        timeout=1800,
    )

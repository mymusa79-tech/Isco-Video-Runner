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
    """Composite visual identity without changing the narration-owned duration."""
    assets = identity_asset_paths(fmt)
    for name, asset in assets.items():
        if not asset.is_file() or asset.stat().st_size <= 1024:
            raise RuntimeError(f"identity asset missing: {name}")

    voice_seconds = float(timeline.get("voice_seconds_measured") or 0.0)
    if voice_seconds <= 0:
        raise RuntimeError("timeline voice duration missing")

    width, height = ((1080, 1920) if fmt == "short" else (1920, 1080))
    card_width = 760 if fmt == "short" else 600

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

    filters = (
        f"[1:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},setsar=1,fps=30,"
        f"setpts=PTS-STARTPTS+{intro_start:.3f}/TB[intro];"
        f"[2:v]scale={card_width}:-1,format=rgba,"
        f"setpts=PTS-STARTPTS+{prayer_start:.3f}/TB[prayer];"
        f"[3:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},setsar=1,fps=30,"
        f"setpts=PTS-STARTPTS+{outro_start:.3f}/TB[outro];"
        f"[0:v][intro]overlay=0:0:enable='between(t,{intro_start:.3f},{intro_end:.3f})'[v1];"
        f"[v1][prayer]overlay=(W-w)/2:(H-h)/2:"
        f"enable='between(t,{prayer_start:.3f},{prayer_end:.3f})'[v2];"
        f"[v2][outro]overlay=0:0:enable='between(t,{outro_start:.3f},{outro_end:.3f})'[vout]"
    )

    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(source),
            "-stream_loop", "-1", "-i", str(assets["intro"]),
            "-loop", "1", "-framerate", "30", "-i", str(assets["prayer"]),
            "-stream_loop", "-1", "-i", str(assets["outro"]),
            "-filter_complex", filters,
            "-map", "[vout]", "-map", "0:a:0",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
            "-pix_fmt", "yuv420p", "-c:a", "copy",
            "-movflags", "+faststart", str(destination),
        ],
        check=True,
        timeout=1800,
    )

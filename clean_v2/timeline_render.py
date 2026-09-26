from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Mapping

from .identity_sequence import PRAYER_SENTENCE, identity_asset_paths


def _event(timeline: Mapping[str, Any], kind: str) -> Mapping[str, Any]:
    rows = timeline.get("identity_events")
    matches = [
        row for row in rows or []
        if isinstance(row, Mapping) and str(row.get("kind") or "") == kind
    ]
    if len(matches) != 1:
        raise RuntimeError(f"timeline identity event invalid: {kind}")
    return matches[0]


def _ass_time(seconds: float) -> str:
    centiseconds = max(0, int(round(float(seconds) * 100.0)))
    hours, remainder = divmod(centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    whole, cs = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{whole:02d}.{cs:02d}"


def _filter_path(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def _prayer_ass(*, fmt: str, start: float, end: float) -> str:
    """Renderer-owned prayer typography over the story world; never a pasted card."""
    if end <= start:
        raise RuntimeError("prayer timeline bounds invalid")
    width, height = ((1080, 1920) if fmt == "short" else (1920, 1080))
    font_size = 92 if fmt == "short" else (70 if fmt == "film" else 66)
    y = 900 if fmt == "short" else 520
    first, second = "اللهم صلِّ وسلِّم", "على نبينا محمد"
    text = (
        "{\\an5\\pos(" + str(width // 2) + "," + str(y) + ")"
        "\\fad(160,200)\\bord3\\shad2\\c&H00F4F2EE&}"
        + first
        + r"\N"
        + "{\\c&H005BA8D7&}"
        + second
    )
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Prayer,Noto Sans Arabic,{font_size},&H00F4F2EE,&H005BA8D7,&H00211E1A,&H50000000,-1,0,0,0,100,100,0,0,1,3,2,5,60,60,0,1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Prayer,,0,0,0,,{text}
"""


def render_identity_composition(
    source: Path,
    destination: Path,
    *,
    fmt: str,
    timeline: Mapping[str, Any],
) -> None:
    """Composite visual identity without changing the narration-owned duration."""
    assets = identity_asset_paths(fmt, runtime_dir=destination.parent / ".identity-assets")
    for name in ("intro", "outro"):
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
    prayer_ass = destination.with_suffix(".prayer.ass")
    prayer_ass.write_text(
        _prayer_ass(fmt=fmt, start=prayer_start, end=prayer_end),
        encoding="utf-8",
    )
    outro_duration = max(0.001, outro_end - outro_start)
    freeze_duration = max(0.0, final_silence_end - final_silence_start)
    filters = (
        f"[0:v]ass='{_filter_path(prayer_ass)}'[base];"
        f"[1:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},setsar=1,fps=30,format=rgba,"
        f"trim=duration={intro_end - intro_start:.3f},"
        f"setpts=PTS-STARTPTS+{intro_start:.3f}/TB[intro];"
        f"[2:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},setsar=1,fps=30,format=rgba,"
        f"trim=duration={outro_duration:.3f},"
        f"tpad=stop_mode=clone:stop_duration={freeze_duration:.3f},"
        f"setpts=PTS-STARTPTS+{outro_start:.3f}/TB[outro];"
        f"[base][intro]overlay=0:0:enable='between(t,{intro_start:.3f},{intro_end:.3f})'[v1];"
        f"[v1][outro]overlay=0:0:"
        f"enable='between(t,{outro_start:.3f},{final_silence_end:.3f})'[vout]"
    )

    try:
        subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(source),
            "-stream_loop", "-1", "-i", str(assets["intro"]),
            "-stream_loop", "-1", "-i", str(assets["outro"]),
            "-filter_complex", filters,
            "-map", "[vout]", "-map", "0:a:0",
            "-t", f"{voice_seconds:.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
            "-pix_fmt", "yuv420p", "-c:a", "copy",
            "-movflags", "+faststart", str(destination),
        ],
        check=True,
        timeout=1800,
        )
    finally:
        prayer_ass.unlink(missing_ok=True)

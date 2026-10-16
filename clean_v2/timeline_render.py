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
    if fmt == "short":
        _render_short_identity(source, destination, timeline=timeline)
        return
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

    # Hold the prayer picture through its existing breath; the separately
    # spoken channel identity gets the intro picture, never leftover stock.
    events = {str(e.get("kind")): e for e in timeline.get("identity_events", []) if isinstance(e, Mapping)}
    if "post_prayer_silence" in events:
        prayer_end = min(voice_seconds, float(events["post_prayer_silence"]["end"]))
    channel_filter = ""
    before_outro = "[v2]"
    if "channel_identity" in events:
        channel_start = float(events["channel_identity"]["start"])
        channel_end = float(events.get("pre_topic_silence", events["channel_identity"])["end"])
        channel_duration = channel_end - channel_start
        channel_filter = (
            f"[1:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1,fps=30,format=rgba,"
            f"tpad=stop_mode=clone:stop_duration={channel_duration:.3f},"
            f"trim=duration={channel_duration:.3f},"
            f"setpts=PTS-STARTPTS+{channel_start:.3f}/TB[channel];"
            f"[v2][channel]overlay=0:0:enable='gte(t,{channel_start:.3f})*lt(t,{channel_end:.3f})'[vchannel];"
        )
        before_outro = "[vchannel]"

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
            f"[2:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps=30,format=rgba,"
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
            f"{channel_filter}{before_outro}[outro]overlay=0:0:"
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
            f"[2:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps=30,format=rgba,"
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
            f"{channel_filter}{before_outro}[outro]overlay=0:0:"
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


def _render_short_identity(source: Path, destination: Path, *, timeline: Mapping[str, Any]) -> None:
    """Short logo appears only during the spoken handoff, without a silent intro."""
    assets = identity_asset_paths("short")
    ala_mahl = timeline.get("template") == "ala_mahl"
    for kind in (("prayer",) if ala_mahl else ("intro", "prayer", "outro")):
        if not assets[kind].is_file() or assets[kind].stat().st_size <= 1024:
            raise RuntimeError(f"identity asset missing: {kind}")
    seconds = float(timeline["voice_seconds_measured"])
    prayer = _event(timeline, "prayer")
    breath = _event(timeline, "post_prayer_silence")
    outro = _event(timeline, "outro")
    channel = None if ala_mahl else _event(timeline, "channel_identity")
    start, end = float(prayer["start"]), float(breath["end"])
    outro_start, outro_end = float(outro["start"]), float(outro["end"])
    channel_start, channel_end = (float(channel["start"]), float(channel["end"])) if channel else (end, end)
    if not (0 <= start < end <= outro_start < outro_end <= seconds + 0.08):
        raise RuntimeError("short identity bounds invalid")
    filters = (
        f"[1:v]scale=1080:1920:force_original_aspect_ratio=decrease,"
        f"pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps=30,"
        f"trim=duration={end-start:.3f},setpts=PTS-STARTPTS+{start:.3f}/TB[prayer];"
        f"[2:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
        f"setsar=1,fps=30,tpad=stop_mode=clone:stop_duration={channel_end-channel_start:.3f},"
        f"trim=duration={channel_end-channel_start:.3f},setpts=PTS-STARTPTS+{channel_start:.3f}/TB[channel];"
        f"[3:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
        f"setsar=1,fps=30,tpad=stop_mode=clone:stop_duration={outro_end-outro_start:.3f},"
        f"trim=duration={outro_end-outro_start:.3f},setpts=PTS-STARTPTS+{outro_start:.3f}/TB[outro];"
        f"[0:v][prayer]overlay=0:0:enable='gte(t,{start:.3f})*lt(t,{end:.3f})'[prayed];"
        f"[prayed][channel]overlay=0:0:enable='gte(t,{channel_start:.3f})*lt(t,{channel_end:.3f})'[branded];"
        f"[branded][outro]overlay=0:0:enable='gte(t,{outro_start:.3f})'[vout]"
    )
    extra_inputs = ["-i", str(assets["intro"]), "-i", str(assets["outro"])]
    if ala_mahl:
        signature = _ala_mahl_signature(destination.parent / ".ala-mahl-signature.png")
        extra_inputs = ["-loop", "1", "-framerate", "30", "-i", str(signature)]
        filters = (
            f"[1:v]scale=1080:1920:force_original_aspect_ratio=decrease,"
            f"pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps=30,"
            f"trim=duration={end-start:.3f},setpts=PTS-STARTPTS+{start:.3f}/TB[prayer];"
            f"[0:v][prayer]overlay=0:0:enable='gte(t,{start:.3f})*lt(t,{end:.3f})'[prayed];"
            f"[prayed][2:v]overlay=0:0:enable='gte(t,{outro_start:.3f})'[vout]"
        )
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(source), "-loop", "1", "-framerate", "30", "-i", str(assets["prayer"]),
        *extra_inputs, "-filter_complex", filters,
        "-map", "[vout]", "-map", "0:a:0", "-t", f"{seconds:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p",
        "-c:a", "copy", "-movflags", "+faststart", str(destination),
    ], check=True, timeout=1800)


def _ala_mahl_signature(destination: Path) -> Path:
    """A small Arabic signature and a drawn cup, avoiding unsupported emoji glyphs."""
    from PIL import Image, ImageDraw, ImageFont
    from .short_timed_text import _caption_font_path

    font = ImageFont.truetype(_caption_font_path(), 76)
    card = Image.new("RGBA", (1080, 1920), (0, 0, 0, 0))
    draw = ImageDraw.Draw(card)
    draw.text((580, 1670), "على مهل", font=font, anchor="mm", direction="rtl", language="ar",
              fill=(245, 243, 239), stroke_width=2, stroke_fill=(11, 18, 32))
    draw.arc((306, 1639, 390, 1705), 0, 180, fill=(245, 243, 239), width=5)
    draw.line((306, 1668, 306, 1639, 390, 1639, 390, 1668), fill=(245, 243, 239), width=5)
    draw.arc((381, 1642, 413, 1678), 265, 100, fill=(245, 243, 239), width=5)
    draw.line((298, 1705, 401, 1705), fill=(245, 243, 239), width=5)
    card.save(destination)
    return destination

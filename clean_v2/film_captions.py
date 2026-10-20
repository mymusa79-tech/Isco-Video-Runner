"""Film-only open captions: editorial typography, not Podcast dialogue cards.

Render verbatim narration phrases inside measured hook/topic audio windows.
The Podcast's speaker cards and the Short's portrait style are untouched.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

from .board_scene import blocked_windows, drop_events_over_boards
from .podcast_captions import build_events as build_measured_events
from .short_timed_text import (
    BODY_FONT,
    OUTLINE_ASS,
    PRIMARY_ASS,
    _ass_time,
    _caption_rows_and_size,
    _filter_escape_path,
    _rtl_row,
    _secret_free_subprocess_env,
    _srt_time,
    resolved_font,
)

FONT_SIZE = 76
TEXT_X = 960
TEXT_BOTTOM_Y = 892
GOLD_ASS = r"&H4BA2C9&"
FONT_OUTLINE = 4
FONT_SHADOW = 2
CLEAN_MASTER_NAME = "final-no-captions.mp4"
CAPTION_NAME = "film-open-captions"


class FilmCaptionError(RuntimeError):
    pass


def build_film_events(output_dir: Path) -> tuple[list[dict[str, Any]], str, int]:
    root = Path(output_dir)
    events, timing = build_measured_events(root, fmt="film")
    if not events:
        raise FilmCaptionError("film_measured_caption_events_missing")
    try:
        timeline = json.loads((root / "timeline-first.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FilmCaptionError("film_caption_timeline_missing") from exc

    identity = timeline.get("identity_events") or []
    hooks = [
        (float(row["start"]), float(row["end"]))
        for row in identity if isinstance(row, Mapping) and row.get("kind") == "hook"
    ]
    if len(hooks) != 1:
        raise FilmCaptionError("film_caption_hook_window_invalid")
    begin, end = hooks[0]
    for item in events:
        item["role"] = "hook" if begin <= float(item["start"]) < end else "topic"

    windows = blocked_windows(root)
    visible = list(drop_events_over_boards(events, windows))
    if not visible:
        raise FilmCaptionError("film_all_caption_events_blocked")
    return visible, timing, len(events) - len(visible)


def build_ass(events: Sequence[Mapping[str, Any]]) -> str:
    """No navy card, speaker labels, forced word karaoke, or background band."""
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "PlayResX: 1920",
        "PlayResY: 1080",
        "YCbCr Matrix: TV.709",
        "",
        "[V4+ Styles]",
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding",
        f"Style: Film,{BODY_FONT},{FONT_SIZE},{PRIMARY_ASS},{PRIMARY_ASS},{OUTLINE_ASS},&H00000000,-1,0,0,0,100,100,0,0,1,{FONT_OUTLINE},{FONT_SHADOW},2,140,140,170,-1",
        "",
        "[Events]",
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
    ]
    for event in events:
        rows, size = _caption_rows_and_size(
            str(event["text"]), fmt="film", font_size=FONT_SIZE
        )
        if not rows:
            continue
        is_hook = str(event.get("role") or "") == "hook"
        visual_rows = []
        for index, row in enumerate(rows):
            # Only the second row of a hook receives gold emphasis; normal
            # narration remains white, rather than highlighting random words.
            colour = GOLD_ASS if is_hook and len(rows) == 2 and index == 1 else PRIMARY_ASS
            visual_rows.append(r"{\1c" + colour + "}" + _rtl_row(row.split()))
        rendered = r"\N".join(visual_rows)
        lines.append(
            f'Dialogue: 0,{_ass_time(float(event["start"]))},{_ass_time(float(event["end"]))},Film,,0,0,0,,'
            + rf"{{\an2\pos({TEXT_X},{TEXT_BOTTOM_Y})\fs{size}\bord{FONT_OUTLINE}\shad{FONT_SHADOW}\blur0.7\fad(100,140)}}"
            + rendered
        )
    return "\n".join(lines) + "\n"


def build_srt(events: Sequence[Mapping[str, Any]]) -> str:
    parts = []
    for index, event in enumerate(events, start=1):
        rows, _ = _caption_rows_and_size(
            str(event["text"]), fmt="film", font_size=FONT_SIZE
        )
        if rows:
            parts.append(
                f'{index}\n{_srt_time(float(event["start"]))} --> {_srt_time(float(event["end"]))}\n'
                + "\n".join(rows) + "\n"
            )
    return "\n".join(parts) + "\n"


def apply_film_captions(
    *, output_dir: Path, final_path: Path,
) -> dict[str, Any]:
    root = Path(output_dir)
    events, timing, board_suppressed = build_film_events(root)
    ass_path = root / (CAPTION_NAME + ".ass")
    srt_path = root / (CAPTION_NAME + ".srt")
    ass_path.write_text(build_ass(events), encoding="utf-8")
    srt_path.write_text(build_srt(events), encoding="utf-8")
    rendered = root / ".final-film-open-captions.mp4"
    try:
        process = subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "verbose",
                "-i", str(Path(final_path).resolve()),
                "-vf", f"ass='{_filter_escape_path(ass_path)}':shaping=complex",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "copy", str(rendered.resolve()),
            ],
            capture_output=True, text=True, check=True, timeout=1800,
            env=_secret_free_subprocess_env(),
        )
        if not rendered.is_file() or rendered.stat().st_size <= 0:
            raise FilmCaptionError("film_captions_render_missing")
        font = resolved_font(process.stderr, BODY_FONT)
        if not font["is_requested_font"]:
            print(
                f"::warning title=film-captions-font::"
                f"libass used {font['resolved_name']} instead of {BODY_FONT}",
                flush=True,
            )
        clean = root / CLEAN_MASTER_NAME
        if not clean.exists():
            shutil.copy2(Path(final_path), clean)
        os.replace(rendered, Path(final_path))
    except (OSError, subprocess.SubprocessError, FilmCaptionError) as exc:
        rendered.unlink(missing_ok=True)
        if isinstance(exc, FilmCaptionError):
            raise
        raise FilmCaptionError(
            f"film_captions_render_failed:{type(exc).__name__}"
        ) from exc

    return {
        "status": "pass",
        "format": "film",
        "renderer": "film_open_typography_cairo_bold_v1",
        "burned_into_final_video": True,
        "event_count": len(events),
        "board_suppressed_event_count": board_suppressed,
        "font": BODY_FONT,
        "font_actually_used": font,
        "font_size": FONT_SIZE,
        "max_rows": 2,
        "position": "lower_center_safe",
        "background_card": False,
        "podcast_style_modified": False,
        "timing": timing,
        "timing_note": "measured audio chunks; within-chunk phrase cuts are estimated or snapped to detected pauses, not word-level alignment",
        "clean_windows": "intro, prayer, definition and outro",
        "sidecars": [ass_path.name, srt_path.name],
        "provider_calls_added": 0,
    }

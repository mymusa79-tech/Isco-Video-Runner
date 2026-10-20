"""Film-only spoken captions: quiet cinematic lower-third, never Podcast dialogue cards.

Only measured hook/topic audio units participate; fixed prayer, definition,
identity and outro windows stay clear. This does not add inference providers.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

from .podcast_captions import build_events
from .short_timed_text import (
    BODY_FONT, _ass_time, _srt_time, _rtl_row, _caption_rows_and_size,
    _accent_word_index, _filter_escape_path, resolved_font, _secret_free_subprocess_env,
)

SCHEMA_VERSION = 1
FONT_SIZE = 66
WHITE_ASS = "&H00EEF2F4"  # warm off-white
GOLD_ASS = "&H004BA2C9"  # restrained identity gold
OUTLINE_ASS = "&H00261A11"  # dark navy
SAFE_BOTTOM_MARGIN = 135
MAX_ROWS = 2


class FilmCaptionError(RuntimeError):
    pass


def build_ass(events: Sequence[Mapping[str, Any]]) -> str:
    """Short voice-timed phrases, no rectangular Podcast backing card."""
    lines = [
        "[Script Info]", "ScriptType: v4.00+", "WrapStyle: 2",
        "ScaledBorderAndShadow: yes", "PlayResX: 1920", "PlayResY: 1080",
        "YCbCr Matrix: TV.709", "",
        "[V4+ Styles]",
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding",
        f"Style: Film,{BODY_FONT},{FONT_SIZE},{WHITE_ASS},{WHITE_ASS},{OUTLINE_ASS},&H00000000,-1,0,0,0,100,100,0,0,1,3,1,2,150,150,{SAFE_BOTTOM_MARGIN},-1",
        "", "[Events]",
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
    ]
    for index, event in enumerate(events):
        start, end = float(event["start"]), float(event["end"])
        if end <= start:
            raise FilmCaptionError("film_caption_invalid_window")
        rows, font_size = _caption_rows_and_size(str(event["text"]), fmt="film", font_size=FONT_SIZE)
        if not rows or len(rows) > MAX_ROWS:
            raise FilmCaptionError("film_caption_invalid_rows")
        # Emphasise a single already-spoken word only occasionally. No sweeps,
        # letter-by-letter animation, cards or artificially invented copy.
        focus = _accent_word_index(str(event["text"])) if index % 7 == 0 else -1
        word_index = 0
        rendered_rows = []
        for row in rows:
            pieces = []
            for word in row.split():
                escaped = _rtl_row([word])
                if word_index == focus:
                    escaped = r"{\1c" + GOLD_ASS + "}" + escaped + r"{\1c" + WHITE_ASS + "}"
                pieces.append(escaped)
                word_index += 1
            rendered_rows.append(" ".join(pieces))
        rendered = r"\N".join(rendered_rows)
        lines.append(
            f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Film,,0,0,0,,"
            + r"{\fs" + str(font_size) + r"\fad(100,130)\blur0.5}"
            + rendered
        )
    return "\n".join(lines) + "\n"


def build_srt(events: Sequence[Mapping[str, Any]]) -> str:
    blocks = []
    for i, event in enumerate(events, 1):
        rows, _size = _caption_rows_and_size(str(event["text"]), fmt="film", font_size=FONT_SIZE)
        if rows:
            blocks.append(
                f'{i}\n{_srt_time(float(event["start"]))} --> {_srt_time(float(event["end"]))}\n'
                + "\n".join(rows) + "\n"
            )
    return "\n".join(blocks) + "\n"


def apply_film_captions(*, output_dir: Path, final_path: Path) -> dict[str, Any]:
    root = Path(output_dir)
    try:
        events, mode = build_events(root, fmt="film", caption_roles=frozenset({"hook", "topic"}))
    except Exception as exc:
        raise FilmCaptionError(f"film_caption_alignment_failed:{type(exc).__name__}") from exc
    if not events:
        raise FilmCaptionError("film_caption_voice_events_missing")
    ass_path = root / "film-captions.ass"
    srt_path = root / "film-captions.srt"
    ass_path.write_text(build_ass(events), encoding="utf-8")
    srt_path.write_text(build_srt(events), encoding="utf-8")
    rendered = root / ".final-film-captions.mp4"
    try:
        completed = subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "verbose",
                "-i", str(Path(final_path).resolve()),
                "-vf", f"ass='{_filter_escape_path(ass_path)}':shaping=complex",
                "-c:v", "libx264", "-preset", "superfast", "-crf", "21",
                "-c:a", "copy", str(rendered.resolve()),
            ],
            check=True, timeout=1800, capture_output=True, text=True,
            env=_secret_free_subprocess_env(),
        )
        font = resolved_font(completed.stderr, BODY_FONT)
        if not rendered.is_file() or rendered.stat().st_size <= 0:
            raise FilmCaptionError("film_caption_render_missing")
        clean = root / "final-no-captions.mp4"
        if not clean.exists():
            shutil.copy2(Path(final_path), clean)
        os.replace(rendered, Path(final_path))
    except FilmCaptionError:
        rendered.unlink(missing_ok=True)
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        rendered.unlink(missing_ok=True)
        raise FilmCaptionError(f"film_caption_render_failed:{type(exc).__name__}") from exc
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "pass", "format": "film",
        "burned_into_final_video": True, "event_count": len(events),
        "font": BODY_FONT, "font_actually_used": font,
        "max_rows": MAX_ROWS, "position": "cinematic_bottom_safe_area",
        "style": "unboxed_offwhite_with_sparse_gold_word_no_podcast_card",
        "timing": mode,
        "timing_note": "voice chunk windows measured, phrase boundaries pause-snapped or character-weighted; not word-level alignment",
        "clean_windows": "prayer, channel identity, structural silences and outro",
        "sidecars": [srt_path.name, ass_path.name],
        "provider_calls_added": 0,
    }

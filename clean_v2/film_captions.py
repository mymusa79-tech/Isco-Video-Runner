"""Film spoken captions, voice-timed, drawn on the navy card with a gold hairline.

The unboxed outlined design that Film used to wear now belongs to the Podcast (see build_plain_ass).

Only measured hook/topic audio units participate; fixed prayer, definition,
identity and outro windows stay clear. This does not add inference providers.
"""
from __future__ import annotations

import os
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
FONT_SIZE = 90  # unboxed design (now worn by the Podcast); was 66, mobile viewers asked for larger
PODCAST_FONT_SIZE = 116  # podcast dialogue captions: the 90 px version read too small on a phone held in portrait
CARD_FONT_SIZE = 84  # card design (now worn by Film): card geometry scales with this size
WHITE_ASS = "&H00EEF2F4"  # warm off-white
GOLD_ASS = "&H004BA2C9"  # restrained identity gold
OUTLINE_ASS = "&H00261A11"  # dark navy
SAFE_BOTTOM_MARGIN = 135
MAX_ROWS = 2


class FilmCaptionError(RuntimeError):
    pass


# A viewer reads about 3-4 Arabic words a second; 40 cues above 6 words/second once shipped as
# an unreadable opening. Anything above this ceiling is a timing bug, never a style choice.
MAX_WORDS_PER_SECOND = 6.0
MIN_EVENT_SECONDS = 0.30
OVERLAP_TOLERANCE = 0.002


def validate_caption_events(events: Sequence[Mapping[str, Any]], *, voice_seconds: float | None = None) -> dict[str, Any]:
    """Objective checks on the voice-timed cue list; raises FilmCaptionError on the first class of failure.

    Sorted, non-overlapping, positive-length, readable speed, inside the voice. Returns the measured
    numbers so the report can show them (never a bare "pass").
    """
    if not events:
        raise FilmCaptionError("film_caption_voice_events_missing")
    rates: list[float] = []
    previous_end = 0.0
    previous_start = -1.0
    for event in events:
        start, end = float(event["start"]), float(event["end"])
        if end <= start or end - start < MIN_EVENT_SECONDS:
            raise FilmCaptionError("film_caption_cue_too_short")
        if start < previous_start - OVERLAP_TOLERANCE:
            raise FilmCaptionError("film_caption_cues_unsorted")
        if start < previous_end - OVERLAP_TOLERANCE:
            raise FilmCaptionError("film_caption_cues_overlap")
        words = len(str(event["text"]).split())
        rate = words / (end - start)
        if rate > MAX_WORDS_PER_SECOND:
            raise FilmCaptionError("film_caption_reading_speed_too_high")
        rates.append(rate)
        previous_start, previous_end = start, end
    if start < 0 or (voice_seconds and previous_end > float(voice_seconds) + 0.5):
        raise FilmCaptionError("film_caption_outside_voice")
    ordered = sorted(rates)
    return {
        "cue_count": len(events),
        "max_words_per_second": round(ordered[-1], 2),
        "median_words_per_second": round(ordered[len(ordered) // 2], 2),
        "first_start": round(float(events[0]["start"]), 3),
        "last_end": round(previous_end, 3),
        "ceiling_words_per_second": MAX_WORDS_PER_SECOND,
    }


def validate_srt_text(text: str, expected_cues: int) -> None:
    """The SRT sidecar must be sequentially numbered, well-formed and time-ordered."""
    import re

    blocks = [b for b in text.strip().split("\n\n") if b.strip()]
    if len(blocks) != expected_cues:
        raise FilmCaptionError("film_caption_srt_cue_count_mismatch")
    stamp = re.compile(r"^(\d{2}):(\d{2}):(\d{2}),(\d{3}) --> (\d{2}):(\d{2}):(\d{2}),(\d{3})$")
    last_end = -1.0
    for number, block in enumerate(blocks, 1):
        lines = block.split("\n")
        match = stamp.match(lines[1]) if len(lines) >= 3 else None
        if lines[0] != str(number) or not match:
            raise FilmCaptionError("film_caption_srt_malformed")
        g = [int(x) for x in match.groups()]
        start = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
        end = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
        if end <= start or start < last_end - OVERLAP_TOLERANCE:
            raise FilmCaptionError("film_caption_srt_time_order")
        last_end = end


def validate_ass_text(text: str, expected_cues: int) -> None:
    """The ASS sidecar must carry one text row per cue on the card design and ordered times."""
    import re

    # Card design: each cue is one text row (layer 3, name "line"); shade/card/rim rows are drawing layers.
    rows = [line for line in text.splitlines() if line.startswith("Dialogue: 3,")]
    if len(rows) != expected_cues:
        raise FilmCaptionError("film_caption_ass_cue_count_mismatch")
    stamp = re.compile(r"^Dialogue: 3,(\d+):(\d{2}):(\d{2})\.(\d{2}),(\d+):(\d{2}):(\d{2})\.(\d{2}),Caption,line,")
    last_end = -1.0
    for row in rows:
        match = stamp.match(row)
        if not match:
            raise FilmCaptionError("film_caption_ass_malformed")
        g = [int(x) for x in match.groups()]
        start = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 100
        end = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 100
        if end <= start or start < last_end - 0.02:
            raise FilmCaptionError("film_caption_ass_time_order")
        last_end = end


def build_ass(events: Sequence[Mapping[str, Any]]) -> str:
    """Film captions: the navy card with a gold hairline, warm white single-narrator text."""
    from .podcast_captions import build_card_ass

    return build_card_ass(events, font_size=CARD_FONT_SIZE, speaker_colours=False)


def build_plain_ass(events: Sequence[Mapping[str, Any]], *, speaker_colours: bool = False,
                    font_size: int = FONT_SIZE, fmt: str = "film") -> str:
    """Short voice-timed phrases, unboxed with a dark outline (the Podcast design).

    With speaker_colours the text keeps the dialogue colours: A gold, B warm white."""
    lines = [
        "[Script Info]", "ScriptType: v4.00+", "WrapStyle: 2",
        "ScaledBorderAndShadow: yes", "PlayResX: 1920", "PlayResY: 1080",
        "YCbCr Matrix: TV.709", "",
        "[V4+ Styles]",
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding",
        f"Style: Film,{BODY_FONT},{font_size},{WHITE_ASS},{WHITE_ASS},{OUTLINE_ASS},&H00000000,-1,0,0,0,100,100,0,0,1,4,1,2,150,150,{SAFE_BOTTOM_MARGIN},-1",
        "", "[Events]",
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
    ]
    for index, event in enumerate(events):
        start, end = float(event["start"]), float(event["end"])
        if end <= start:
            raise FilmCaptionError("film_caption_invalid_window")
        rows, row_size = _caption_rows_and_size(str(event["text"]), fmt=fmt, font_size=font_size)
        if not rows or len(rows) > MAX_ROWS:
            raise FilmCaptionError("film_caption_invalid_rows")
        # Emphasise a single already-spoken word only occasionally. No sweeps,
        # letter-by-letter animation, cards or artificially invented copy.
        focus = _accent_word_index(str(event["text"])) if index % 7 == 0 and not speaker_colours else -1
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
        colour = ""
        if speaker_colours:
            from .podcast_captions import SPEAKER_ASS

            colour = SPEAKER_ASS.get(str(event.get("speaker") or ""), "")
        lines.append(
            f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Film,,0,0,0,,"
            + r"{\fs" + str(row_size) + r"\fad(100,130)\blur0.5" + colour + "}"
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
    # Never remove spoken subtitles from the downloadable SRT: viewers relying
    # on captions need the full narration even while a board occupies the frame.
    all_spoken_events = list(events)
    # The burned-in ASS alone avoids overprinting the explanatory board.
    from .board_scene import blocked_windows, drop_events_over_boards
    board_windows = blocked_windows(root)
    events = drop_events_over_boards(all_spoken_events, board_windows)
    voice_seconds = None
    try:
        import json

        voice_seconds = float(json.loads((root / "timeline-first.json").read_text(encoding="utf-8")).get("voice_seconds_measured") or 0) or None
    except (OSError, ValueError, TypeError):
        voice_seconds = None
    quality = validate_caption_events(all_spoken_events, voice_seconds=voice_seconds)
    visual_quality = (
        validate_caption_events(events, voice_seconds=voice_seconds)
        if events else {"cue_count": 0, "all_cues_suppressed_by_board": True}
    )
    ass_path = root / "film-captions.ass"
    srt_path = root / "film-captions.srt"
    ass_text, srt_text = build_ass(events), build_srt(all_spoken_events)
    validate_ass_text(ass_text, len(events))
    validate_srt_text(srt_text, len(all_spoken_events))
    ass_path.write_text(ass_text, encoding="utf-8")
    srt_path.write_text(srt_text, encoding="utf-8")
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
        "burned_into_final_video": bool(events), "event_count": len(events),
        "srt_event_count": len(all_spoken_events),
        "board_suppressed_visual_cues": len(all_spoken_events) - len(events),
        "subtitles_keep_spoken_board_windows": True,
        "font": BODY_FONT, "font_actually_used": font,
        "max_rows": MAX_ROWS, "position": "cinematic_bottom_safe_area",
        "style": "navy_card_gold_hairline_single_narrator",
        "timing": mode,
        "quality": quality,
        "visual_caption_quality": visual_quality,
        "timing_note": "voice chunk windows measured, phrase boundaries pause-snapped or character-weighted; not word-level alignment",
        "clean_windows": "prayer, channel identity, structural silences and outro",
        "sidecars": [srt_path.name, ass_path.name],
        "provider_calls_added": 0,
    }

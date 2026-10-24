"""Full burned-in Podcast captions, coloured per speaker.

The Podcast is a two-voice conversation, so a viewer watching without sound must see
who is talking: A (the listener's question) in gold, B (the answer) in warm white.

Timing is owned by the measured audio, never guessed from the section:
  * every spoken chunk has a measured start/end in ``timeline-first.json``;
  * the chunk text is saved next to its audio in ``voice-sections.json``;
  * inside one chunk the phrase boundaries are snapped to the real pauses found in that
    chunk's own audio file (ffmpeg silencedetect), falling back to character weights.
This is not word-level speech alignment and the report says so.

Captions are shown only over the hook question and the topic. The fixed intro, prayer,
channel definition, CTA and outro stay clean, and the sparse key text is not drawn on
top of them (the pipeline skips it when these captions are burned).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

from .short_timed_text import (
    BODY_FONT,
    OUTLINE_ASS,
    PRIMARY_ASS,
    _ass_time,
    _caption_rows_and_size,
    _filter_escape_path,
    _rtl_row,
    is_verbatim_split,
    resolved_font,
    _secret_free_subprocess_env,
    _speaker_chunks,
    _srt_time,
)

CLEAN_MASTER_NAME = "final-no-captions.mp4"
SCHEMA_VERSION = 1
CAPTION_ROLES = frozenset({"hook", "topic"})  # cta_topic keeps its own on-screen CTA
SPEAKER_HEX = {"A": "#C9A24B", "B": "#F5F3EF"}
SPEAKER_ASS = {"A": r"\c&H4BA2C9&", "B": r"\c&HEFF3F5&"}
FONT_SIZE = 78  # ~7.2% of the 1080 px frame (72 before); two rows still fit the safe width
MARGIN_V = 150
SHORT_CAPTION_FONT_SIZE = 128  # was 92: too small on a phone; still well short of filling the frame
SHORT_CAPTION_MARGIN_V = 570   # bottom edge of the text at y=1350 of 1920 (was 1490, under YouTube's UI)
# Rounded semi-transparent charcoal card behind long-form captions. It is centred
# horizontally and sized per run of captions: one row gets a slim card, two rows the
# full one. Text is centred INSIDE the card (an5), so one-line captions no longer sit
# at the bottom edge of an oversized band. The card's bottom edge stays >= 115 px above
# the frame bottom so player controls / progress bars never touch it.
BAR_LEFT = 150
BAR_RIGHT = 1770
BAR_CENTER_Y = 826
BAR_ROW_HEIGHT = 125  # Cairo line box at FONT_SIZE
BAR_PAD = 14
BAR_RADIUS = 34
BAR_ALPHA_ASS = "&H38&"  # ~78% opaque
BAR_COLOUR_ASS = "&H26160E&"  # deep navy #0E1626 (BGR), the channel identity blue
GOLD_ASS = "&H4BA2C9&"  # #C9A24B, identity gold (BGR)
SPEAKER_ACCENT_ASS = {"A": "&H4BA2C9&", "B": "&HEFF3F5&"}
BAR_ACCENT_INSET = 30
BAR_ACCENT_WIDTH = 8
BAR_TOP = BAR_CENTER_Y - (2 * BAR_ROW_HEIGHT + 2 * BAR_PAD) // 2
BAR_BOTTOM = BAR_CENTER_Y + (2 * BAR_ROW_HEIGHT + 2 * BAR_PAD) // 2
BAR_MERGE_GAP_SECONDS = 0.35
MIN_EVENT_SECONDS = 0.5
SILENCE_NOISE_DB = -32
SILENCE_MIN_SECONDS = 0.18


class PodcastCaptionError(RuntimeError):
    pass


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _silence_midpoints(audio: Path) -> list[float]:
    """Pause centres (seconds, relative to the file) found in one chunk's audio."""
    try:
        completed = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-nostats", "-i", str(audio),
                "-af", f"silencedetect=noise={SILENCE_NOISE_DB}dB:d={SILENCE_MIN_SECONDS}",
                "-f", "null", "-",
            ],
            capture_output=True, text=True, timeout=300, check=False,
            env=_secret_free_subprocess_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return []
    starts = [float(v) for v in re.findall(r"silence_start:\s*(-?[0-9.]+)", completed.stderr)]
    ends = [float(v) for v in re.findall(r"silence_end:\s*(-?[0-9.]+)", completed.stderr)]
    return [(max(s, 0.0) + e) / 2 for s, e in zip(starts, ends) if e > s]


def align_boundaries(weights: Sequence[int], duration: float, pauses: Sequence[float]) -> list[float]:
    """Cut points (len(weights)+1, from 0 to duration), snapped to real pauses when close.

    Each cut starts from the character-weighted estimate over the time that is still left
    (so a snap re-anchors the rest instead of letting error pile up) and moves to a detected
    pause only if one lies within about half a phrase of the estimate. A far-away pause is
    more likely another sentence gap than this boundary, so it is ignored.
    """
    cuts = [0.0]
    count = len(weights)
    for index in range(count - 1):
        remaining_weight = float(sum(weights[index:])) or 1.0
        remaining_time = duration - cuts[-1]
        phrase = remaining_time * weights[index] / remaining_weight
        target = cuts[-1] + phrase
        tolerance = min(2.5, max(0.6, 0.45 * phrase))
        floor = cuts[-1] + MIN_EVENT_SECONDS
        ceiling = duration - MIN_EVENT_SECONDS * (count - index - 1)
        near = [p for p in pauses if floor <= p <= ceiling and abs(p - target) <= tolerance]
        cut = min(near, key=lambda p: abs(p - target)) if near else target
        cuts.append(min(max(cut, floor), max(floor, ceiling)))
    cuts.append(duration)
    return cuts


def align_phrases(
    tagged: Sequence[tuple[str, str]], duration: float, pauses: Sequence[float]
) -> list[tuple[str, str, float, float]]:
    """(speaker, phrase, start, end) inside one chunk.

    Turn changes are aligned first, against all the pauses: that is where the audio has a
    reliable gap. Phrase cuts inside a turn then use only the pauses that fall inside that turn,
    so a turn-change pause can never be mistaken for a mid-sentence boundary.
    """
    turns: list[list[int]] = []
    for index, (speaker, _chunk) in enumerate(tagged):
        if turns and tagged[turns[-1][-1]][0] == speaker:
            turns[-1].append(index)
        else:
            turns.append([index])
    turn_weights = [sum(max(1, len(tagged[i][1])) for i in group) for group in turns]
    turn_cuts = align_boundaries(turn_weights, duration, pauses)
    placed: list[tuple[str, str, float, float]] = []
    for group, left, right in zip(turns, turn_cuts, turn_cuts[1:]):
        inner = [p - left for p in pauses if left + 0.3 < p < right - 0.3]
        cuts = align_boundaries([max(1, len(tagged[i][1])) for i in group], right - left, inner)
        for i, a, b in zip(group, cuts, cuts[1:]):
            placed.append((tagged[i][0], tagged[i][1], left + a, left + b))
    return placed


def build_events(output_dir: Path, *, fmt: str = "podcast", caption_roles=CAPTION_ROLES) -> tuple[list[dict[str, Any]], str]:
    root = Path(output_dir)
    timeline = _read_json(root / "timeline-first.json")
    voice = _read_json(root / "voice-sections.json")
    if not isinstance(timeline, Mapping) or timeline.get("status") != "pass":
        raise PodcastCaptionError("podcast_captions_timeline_missing")
    if not isinstance(voice, Mapping):
        raise PodcastCaptionError("podcast_captions_voice_report_missing")
    texts: dict[tuple[str, int], str] = {}
    for section in voice.get("sections") or []:
        if not isinstance(section, Mapping):
            continue
        for chunk in section.get("chunks") or []:
            if isinstance(chunk, Mapping) and chunk.get("text"):
                texts[(str(section.get("id")), int(chunk.get("chunk") or 0))] = str(chunk["text"])

    events: list[dict[str, Any]] = []
    snapped = 0
    for unit in timeline.get("audio_units") or []:
        if not isinstance(unit, Mapping) or str(unit.get("role")) not in caption_roles:
            continue
        text = texts.get((str(unit.get("section_id")), int(unit.get("chunk") or 0)))
        if not text:
            raise PodcastCaptionError("podcast_captions_chunk_text_missing")
        start, end = float(unit["start"]), float(unit["end"])
        if end - start < MIN_EVENT_SECONDS:
            continue
        tagged = [(speaker, chunk) for speaker, chunk in _speaker_chunks(text, fmt) if chunk.strip()]
        if not tagged:
            continue
        if not is_verbatim_split([chunk for _speaker, chunk in tagged], text):
            # The approved text is the only truth: a caption set that added, lost or changed
            # a word is never drawn.
            raise PodcastCaptionError("podcast_captions_text_not_verbatim")
        audio = root / str(unit.get("file") or "")
        pauses = _silence_midpoints(audio) if audio.is_file() else []
        scale = (end - start) / max(float(unit.get("duration_seconds") or (end - start)), 1e-6)
        pauses = [p * scale for p in pauses]
        placed = align_phrases(tagged, end - start, pauses)
        if pauses and any(abs(a - p) < 0.05 for _s, _c, a, _b in placed[1:] for p in pauses):
            snapped += 1
        for speaker, chunk, left, right in placed:
            if right - left > 0.01:
                events.append({
                    "start": round(start + left, 3), "end": round(start + right, 3),
                    "text": chunk, "speaker": speaker,
                })
    mode = "measured_audio_units_snapped_to_pauses" if snapped else "measured_audio_units_char_weighted"
    return events, mode


def _rounded_card(left: int, top: int, right: int, bottom: int, radius: int) -> str:
    """ASS vector path of a rounded rectangle (quarter-circle Bezier corners)."""
    r = radius
    k = int(round(r * 0.5523))
    return (
        f"m {left + r} {top} l {right - r} {top} "
        f"b {right - r + k} {top} {right} {top + r - k} {right} {top + r} "
        f"l {right} {bottom - r} "
        f"b {right} {bottom - r + k} {right - r + k} {bottom} {right - r} {bottom} "
        f"l {left + r} {bottom} "
        f"b {left + r - k} {bottom} {left} {bottom - r + k} {left} {bottom - r} "
        f"l {left} {top + r} "
        f"b {left} {top + r - k} {left + r - k} {top} {left + r} {top}"
    )


def build_ass(events: Sequence[Mapping[str, Any]], *, fmt: str = "podcast") -> str:
    """Podcast uses the unboxed outlined design (the one Film used to wear); Short keeps its thick outline.

    The navy card design now belongs to Film: see build_card_ass.
    """
    if fmt != "short":
        from .film_captions import PODCAST_FONT_SIZE, build_plain_ass

        return build_plain_ass(events, speaker_colours=True, font_size=PODCAST_FONT_SIZE, fmt="podcast_plain")
    return _build_short_ass(events)


def _build_short_ass(events: Sequence[Mapping[str, Any]]) -> str:
    fmt = "short"
    width, height, font_size, margin = 1080, 1920, SHORT_CAPTION_FONT_SIZE, SHORT_CAPTION_MARGIN_V
    side_margin = 110
    lines = [
        "[Script Info]", "ScriptType: v4.00+", "WrapStyle: 2", "ScaledBorderAndShadow: yes",
        f"PlayResX: {width}", f"PlayResY: {height}", "YCbCr Matrix: TV.709", "",
        "[V4+ Styles]",
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding",
        f"Style: Caption,{BODY_FONT},{font_size},{PRIMARY_ASS},{PRIMARY_ASS},{OUTLINE_ASS},&H00000000,-1,0,0,0,100,100,0,0,1,3,0,2,{side_margin},{side_margin},{margin},-1",
        "", "[Events]",
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
    ]
    for item in events:
        rows, size = _caption_rows_and_size(str(item["text"]), fmt=fmt, font_size=font_size)
        if not rows:
            continue
        rendered = r"\N".join(_rtl_row(row.split()) for row in rows)
        colour = SPEAKER_ASS.get(str(item.get("speaker") or ""), "")
        start, end = float(item["start"]), float(item["end"])
        lines.append(
            f'Dialogue: 1,{_ass_time(start)},{_ass_time(end)},Caption,,0,0,0,,'
            + r"{\fs" + str(size) + r"\bord7\3c&H00140B07&\4c&H00000000&\4a&H70&\shad4\blur0.8"
            + r"\fad(100,120)" + colour + "}" + rendered
        )
    lines.append("")
    return "\n".join(lines)


def build_card_ass(events: Sequence[Mapping[str, Any]], *, font_size: int = FONT_SIZE, speaker_colours: bool = True) -> str:
    """Navy rounded card with a gold hairline, one fixed card per run of consecutive captions.

    Geometry was drawn for FONT_SIZE; a larger font scales the card's row height with it.
    Without speakers (Film narration) the text is warm white and the accent bar stays off.
    """
    width, height, margin = 1920, 1080, MARGIN_V
    scale = font_size / FONT_SIZE
    row_height = round(BAR_ROW_HEIGHT * scale)
    lines = [
        "[Script Info]", "ScriptType: v4.00+", "WrapStyle: 2", "ScaledBorderAndShadow: yes",
        f"PlayResX: {width}", f"PlayResY: {height}", "YCbCr Matrix: TV.709", "",
        "[V4+ Styles]",
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding",
        f"Style: Caption,{BODY_FONT},{font_size},{PRIMARY_ASS},{PRIMARY_ASS},{OUTLINE_ASS},&H00000000,-1,0,0,0,100,100,0,0,1,3,0,2,140,140,{margin},-1",
        "", "[Events]",
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
    ]
    spans: list[list[float]] = []  # [start, end, max_rows]
    for item in events:
        rows, size = _caption_rows_and_size(str(item["text"]), fmt="podcast", font_size=font_size)
        if not rows:
            continue
        rendered = r"\N".join(_rtl_row(row.split()) for row in rows)
        speaker = str(item.get("speaker") or "") if speaker_colours else ""
        colour = SPEAKER_ASS.get(speaker, "")
        start, end = float(item["start"]), float(item["end"])
        if spans and start - spans[-1][1] <= BAR_MERGE_GAP_SECONDS:
            spans[-1][1] = max(spans[-1][1], end)
            spans[-1][2] = max(spans[-1][2], len(rows))
        else:
            spans.append([start, end, len(rows)])
        # Layers: 0 soft shadow, 1 navy card, 2 gold hairline + speaker accent, 3 text.
        lines.append(
            f'Dialogue: 3,{_ass_time(start)},{_ass_time(end)},Caption,line,0,0,0,,'
            + r"{\an5\move(960," + str(BAR_CENTER_Y + 10) + r",960," + str(BAR_CENTER_Y) + r",0,200)"
            + r"\fs" + str(size) + r"\bord2\blur0.6\fad(120,120)" + colour + "}" + rendered
        )
        accent = SPEAKER_ACCENT_ASS.get(speaker, "")
        if accent:
            half_bar = int(min(len(rows), 2) * row_height * 0.3)
            x1 = BAR_RIGHT - BAR_ACCENT_INSET
            shape = _rounded_card(x1 - BAR_ACCENT_WIDTH, BAR_CENTER_Y - half_bar, x1, BAR_CENTER_Y + half_bar, BAR_ACCENT_WIDTH // 2)
            lines.append(
                f"Dialogue: 2,{_ass_time(start)},{_ass_time(end)},Caption,accent,0,0,0,,"
                + r"{\an7\pos(0,0)\fad(120,120)\bord0\shad0\1c" + accent + r"\1a&H20&\p1}" + shape + r"{\p0}"
            )
    for start, end, rows in spans:
        half = (min(rows, 2) * row_height + 2 * BAR_PAD) // 2
        top, bottom = BAR_CENTER_Y - half, BAR_CENTER_Y + half
        shape = _rounded_card(BAR_LEFT, top, BAR_RIGHT, bottom, BAR_RADIUS)
        shade = _rounded_card(BAR_LEFT, top + 10, BAR_RIGHT, bottom + 10, BAR_RADIUS)
        window = f"{_ass_time(start)},{_ass_time(end)}"
        lines.append(
            f"Dialogue: 0,{window},Caption,shade,0,0,0,,"
            + r"{\an7\pos(0,0)\fad(120,140)\bord0\shad0\blur16\1c&H000000&\1a&H8C&\p1}" + shade + r"{\p0}"
        )
        lines.append(
            f"Dialogue: 1,{window},Caption,card,0,0,0,,"
            + r"{\an7\pos(0,0)\fad(120,140)\bord0\shad0\1c" + BAR_COLOUR_ASS + r"\1a" + BAR_ALPHA_ASS + r"\p1}" + shape + r"{\p0}"
        )
        lines.append(
            f"Dialogue: 2,{window},Caption,rim,0,0,0,,"
            + r"{\an7\pos(0,0)\fad(120,140)\shad0\1a&HFF&\bord2\3c" + GOLD_ASS + r"\3a&H80&\p1}" + shape + r"{\p0}"
        )
    lines.append("")
    return "\n".join(lines)


def build_srt(events: Sequence[Mapping[str, Any]]) -> str:
    blocks = []
    for index, item in enumerate(events, 1):
        rows, _size = _caption_rows_and_size(str(item["text"]), fmt="podcast", font_size=FONT_SIZE)
        if rows:
            blocks.append(
                f'{index}\n{_srt_time(float(item["start"]))} --> {_srt_time(float(item["end"]))}\n'
                + "\n".join(rows) + "\n"
            )
    return "\n".join(blocks) + "\n"


def apply_podcast_captions(*, output_dir: Path, final_path: Path) -> dict[str, Any]:
    root = Path(output_dir)
    events, timing = build_events(root)
    if not events:
        return {"status": "skipped_no_caption_windows", "format": "podcast", "provider_calls_added": 0}
    ass_path = root / "podcast-captions.ass"
    srt_path = root / "podcast-captions.srt"
    ass_path.write_text(build_ass(events), encoding="utf-8")
    srt_path.write_text(build_srt(events), encoding="utf-8")
    rendered = root / ".final-podcast-captions.mp4"
    try:
        completed = subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "verbose",
                "-i", str(Path(final_path).resolve()),
                "-vf", f"ass='{_filter_escape_path(ass_path)}':shaping=complex",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "copy", str(rendered.resolve()),
            ],
            check=True, timeout=1800, env=_secret_free_subprocess_env(),
            capture_output=True, text=True,
        )
        font = resolved_font(completed.stderr, BODY_FONT)
        if not font["is_requested_font"]:
            print(f"::warning title=podcast-captions-font::libass drew with {font['resolved_name']}, not {BODY_FONT}")
        if not rendered.is_file() or rendered.stat().st_size <= 0:
            raise PodcastCaptionError("podcast_captions_render_missing_or_empty")
        # Keep the caption-free master: the derived Short is cut from it so the long
        # captions are never burned a second time inside the 9:16 clip (and its blurred
        # background). Not a delivered file and not part of the primary hash set.
        shutil.copy2(Path(final_path), root / CLEAN_MASTER_NAME)
        os.replace(rendered, Path(final_path))
    except PodcastCaptionError:
        rendered.unlink(missing_ok=True)
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        rendered.unlink(missing_ok=True)
        raise PodcastCaptionError(f"podcast_captions_render_failed:{type(exc).__name__}") from exc
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "pass",
        "format": "podcast",
        "burned_into_final_video": True,
        "event_count": len(events),
        "speaker_colors": SPEAKER_HEX,
        "font": BODY_FONT,
        "font_actually_used": font,
        "text_policy": "approved script words only; split into phrases, verified word-for-word before render",
        "font_weight": "bold",
        "max_rows": 2,
        "position": "bottom_safe_area",
        "timing": timing,
        "timing_note": "chunk windows are measured audio; phrase cuts inside a chunk are snapped to detected pauses, not word-level speech alignment",
        "clean_windows": "intro, prayer, channel definition, CTA and outro have no dialogue captions",
        "sidecars": [srt_path.name, ass_path.name],
        "provider_calls_added": 0,
        "new_quality_gate": False,
    }

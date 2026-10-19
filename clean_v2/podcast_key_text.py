from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

from clean_v2.short_timed_text import (
    BODY_FONT,
    BODY_FONT_SIZE,
    EXTRUSION_ASS,
    OUTLINE_ASS,
    PRIMARY_ASS,
    SHADOW_ASS,
    _accent_caption,
    _accent_word_index,
    _ass_escape,
    _ass_time,
    _filter_escape_path,
    occurs_in_source,
    resolved_font,
    _caption_rows_and_size,
    _caption_width,
    _rtl_row,
    _clean as _shared_arabic_clean,
    _secret_free_subprocess_env,
)

SCHEMA_VERSION = 1
RENDERER_VERSION = "clean-v2-sparse-key-text-cairo-bold-v4"
MAX_EVENTS = 3
FONT_SIZE = 94
HOOK_FONT_SIZE = 108
PAYOFF_FONT_SIZE = 100
TEXT_X = 960
TEXT_Y = 770
EXTRUDE = (2, 3)
SHADOW = (4, 5)
DISPLAY_SECONDS = 4.2
MAX_WORDS = 10

FILM_MAX_EVENTS = 5
FILM_FONT_SIZE = 100
FILM_HOOK_FONT_SIZE = 116
FILM_PAYOFF_FONT_SIZE = 106
FILM_TEXT_Y = 760
FILM_DISPLAY_SECONDS = 5.0
FILM_MAX_WORDS = 10
FILM_MIN_GAP_SECONDS = 12.0
TRANSITION_MARKERS = ("لكن", "المشكلة", "الحقيقة", "ربما", "وهنا", "لأن", "لهذا")
_PRAYER_TEXT_MARKERS = ("اللهم", "محمد")


def _contains_prayer_text(value: object) -> bool:
    compact = _clean(value)
    return all(marker in compact for marker in _PRAYER_TEXT_MARKERS)


class PodcastKeyTextError(RuntimeError):
    pass


def _clean(value: object) -> str:
    return _shared_arabic_clean(value)


def _strip_speaker_label(value: object) -> str:
    return re.sub(r"^(?:A|B):\s*", "", _clean(value)).strip()


def _sentences(value: object) -> list[str]:
    text = _clean(value)
    if not text:
        return []
    return [
        _strip_speaker_label(part)
        for part in re.split(r"(?<=[.!؟!])\s+", text)
        if part.strip() and not _contains_prayer_text(part)
    ]


def _dialogue_candidates(value: object, *, max_words: int) -> dict[str, list[str]]:
    source = _clean(value)
    matches = list(re.finditer(r"(?:^|\s)([AB]):\s+", source))
    result: dict[str, list[str]] = {"A": [], "B": []}
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(source)
        turn = _clean(source[start:end])
        for sentence in _sentences(turn):
            words = len(sentence.split())
            if 3 <= words <= max_words:
                result[match.group(1)].append(sentence)
    return result


def _compact_candidates(value: object, *, max_words: int = MAX_WORDS) -> list[str]:
    """Select complete display sentences without exposing internal A:/B: labels."""
    candidates: list[str] = []
    for sentence in _sentences(value):
        display = re.sub(r"^[AB]:\s*", "", sentence, flags=re.I).strip()
        words = len(display.split())
        if 3 <= words <= max_words:
            candidates.append(display)
    return candidates


def _pick_text(
    narration: str,
    role: str,
    *,
    closer: str = "",
    max_words: int = MAX_WORDS,
) -> str:
    text = _clean(narration)
    closer = _clean(closer)
    if closer and text.endswith(closer):
        text = text[: -len(closer)].strip()
    dialogue = _dialogue_candidates(text, max_words=max_words)
    candidates = _compact_candidates(text, max_words=max_words)
    if role == "hook" and dialogue["A"]:
        return dialogue["A"][0]
    if role == "turn" and dialogue["A"]:
        return dialogue["A"][0]
    if role == "payoff" and dialogue["B"]:
        return dialogue["B"][-1]
    if not candidates:
        return ""
    if role == "hook":
        return candidates[0]
    if role == "turn":
        return next(
            (item for item in candidates if any(marker in item for marker in TRANSITION_MARKERS)),
            candidates[0],
        )
    return candidates[-1]


MIN_SPOKEN_SECONDS = 1.2


def _norm(value: object) -> str:
    return " ".join(_clean(value).split())


def locate_spoken_window(
    text: str,
    cues: Sequence[Mapping[str, Any]],
    *,
    near: tuple[float, float],
    display_seconds: float = 0.0,
) -> tuple[float, float] | None:
    """Start/end of ``text`` inside the measured speech cues, or None when it is not there.

    The words are matched as one unbroken run of the cue words (speaker labels ignored);
    times inside a cue are interpolated by word position. When the run occurs more than
    once, the occurrence nearest to the section window wins. The line stays on screen for
    exactly as long as it is spoken, never before and never after.
    """
    words = _norm(text).split()
    if not words:
        return None
    stream: list[tuple[str, float, float]] = []  # word, start, end
    for cue in cues:
        cue_words = _norm(_strip_speaker_label(cue.get("text"))).split()
        if not cue_words:
            continue
        start, end = float(cue["start"]), float(cue["end"])
        step = (end - start) / len(cue_words)
        for position, word in enumerate(cue_words):
            stream.append((word, start + position * step, start + (position + 1) * step))
    flat = [item[0] for item in stream]
    best: tuple[float, float, float] | None = None
    for index in range(len(flat) - len(words) + 1):
        if flat[index:index + len(words)] != words:
            continue
        start, end = stream[index][1], stream[index + len(words) - 1][2]
        centre = (start + end) / 2
        distance = 0.0 if near[0] <= centre <= near[1] else min(abs(centre - near[0]), abs(centre - near[1]))
        if best is None or distance < best[0]:
            best = (distance, start, end)
    if best is None or best[0] > 1.0:
        return None  # not spoken inside its own section
    _distance, start, end = best
    return start, end


def _seconds(value: object, label: str) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        raise PodcastKeyTextError(f"podcast_key_text_{label}_invalid") from None
    if seconds < 0:
        raise PodcastKeyTextError(f"podcast_key_text_{label}_invalid")
    return seconds


def _identity_text_window(
    timeline: Mapping[str, Any],
    *,
    kind: str,
) -> tuple[float, float] | None:
    """Return one text-safe identity window.

    Sparse key text is allowed only over the spoken hook or topic. Intro,
    prayer, channel identity, structural silences and outro remain clean.
    """
    raw = timeline.get("identity_events")
    if not isinstance(raw, list):
        return None
    for item in raw:
        if not isinstance(item, Mapping) or str(item.get("kind") or "") != kind:
            continue
        start = _seconds(item.get("start"), f"{kind}_start")
        end = _seconds(item.get("end"), f"{kind}_end")
        if end > start:
            return start, end
    return None


def _film_selected_indices(section_count: int, total_seconds: float) -> list[int]:
    target = 3 if total_seconds < 240.0 else (4 if total_seconds < 420.0 else 5)
    target = min(target, section_count, FILM_MAX_EVENTS)
    if target <= 1:
        return [0]
    return sorted({
        int(round(position * (section_count - 1) / (target - 1)))
        for position in range(target)
    })


def build_events(
    *,
    script: Mapping[str, Any],
    timeline: Mapping[str, Any],
    closer: str = "",
    fmt: str = "podcast",
    cues: Sequence[Mapping[str, Any]] | None = None,
) -> list[dict[str, object]]:
    """Pick the key lines. With ``cues`` (measured speech phrases) every line is shown
    exactly while it is spoken; a line that cannot be located in the speech is dropped."""
    if fmt not in {"podcast", "film"}:
        raise PodcastKeyTextError(f"sparse_key_text_format_invalid:{fmt}")

    sections = script.get("sections")
    section_events = timeline.get("section_events")
    if (
        timeline.get("status") != "pass"
        or not isinstance(sections, list)
        or len(sections) < 2
        or not isinstance(section_events, list)
        or len(section_events) != len(sections)
    ):
        raise PodcastKeyTextError("podcast_key_text_timeline_invalid")

    raw_identity = timeline.get("identity_events")
    identity = [item for item in raw_identity if isinstance(item, Mapping)] if isinstance(raw_identity, list) else []
    hook_window = next((item for item in identity if str(item.get("kind") or "") == "hook"), None)
    topic_window = next((item for item in identity if str(item.get("kind") or "") == "topic"), None)
    pre_outro_window = next(
        (item for item in identity if str(item.get("kind") or "") == "pre_outro_silence"),
        None,
    )
    outro_window = next((item for item in identity if str(item.get("kind") or "") == "outro"), None)

    if fmt == "podcast":
        selected = (
            [(0, "hook"), (1, "payoff")]
            if len(sections) == 2
            else [(0, "hook"), (len(sections) // 2, "turn"), (len(sections) - 1, "payoff")]
        )
        display_seconds = DISPLAY_SECONDS
        max_words = MAX_WORDS
        max_events = MAX_EVENTS
    else:
        total_seconds = _seconds(section_events[-1].get("end"), "film_total_end")
        indices = _film_selected_indices(len(sections), total_seconds)
        selected = [
            (
                index,
                "hook" if position == 0 else ("payoff" if position == len(indices) - 1 else "turn"),
            )
            for position, index in enumerate(indices)
        ]
        display_seconds = FILM_DISPLAY_SECONDS
        max_words = FILM_MAX_WORDS
        max_events = FILM_MAX_EVENTS

    events: list[dict[str, object]] = []
    used_texts: set[str] = set()
    previous_end = -999.0
    for index, role in selected:
        section = sections[index]
        timing = section_events[index]
        if not isinstance(section, Mapping) or not isinstance(timing, Mapping):
            raise PodcastKeyTextError("podcast_key_text_section_invalid")
        if str(timing.get("section_id") or "") != str(section.get("id") or ""):
            raise PodcastKeyTextError("podcast_key_text_section_order_invalid")

        text = _pick_text(
            str(section.get("narration") or ""),
            role,
            closer=closer if role == "payoff" else "",
            max_words=max_words,
        )
        if not text:
            continue
        if _norm(text) in used_texts:
            continue  # the same line is never shown twice

        section_start = _seconds(timing.get("start"), "start")
        section_end = _seconds(timing.get("end"), "end")
        if section_end <= section_start:
            raise PodcastKeyTextError("podcast_key_text_duration_invalid")

        if cues is not None:
            spoken = locate_spoken_window(
                text, cues, near=(section_start, section_end), display_seconds=display_seconds
            )
            if spoken is None:
                continue
            start_seconds, end_seconds = spoken
            if fmt == "film" and start_seconds - previous_end < FILM_MIN_GAP_SECONDS:
                continue
            if end_seconds - start_seconds >= MIN_SPOKEN_SECONDS:
                events.append({
                    "start": round(start_seconds, 3), "end": round(end_seconds, 3),
                    "text": text, "role": role, "format": fmt, "timing": "spoken_window",
                })
                used_texts.add(_norm(text))
                previous_end = end_seconds
            continue

        if role == "hook" and isinstance(hook_window, Mapping):
            allowed_start = _seconds(hook_window.get("start"), "hook_start")
            allowed_end = _seconds(hook_window.get("end"), "hook_end")
            start_seconds = max(section_start, allowed_start)
            end_seconds = min(section_end, allowed_end, start_seconds + display_seconds)
        elif role == "payoff":
            allowed_start = (
                _seconds(topic_window.get("start"), "topic_start")
                if isinstance(topic_window, Mapping)
                else section_start
            )
            allowed_end = (
                _seconds(topic_window.get("end"), "topic_end")
                if isinstance(topic_window, Mapping)
                else section_end
            )
            if isinstance(pre_outro_window, Mapping):
                allowed_end = min(
                    allowed_end,
                    _seconds(pre_outro_window.get("start"), "pre_outro_start"),
                )
            elif isinstance(outro_window, Mapping):
                allowed_end = min(
                    allowed_end,
                    _seconds(outro_window.get("start"), "outro_start") - 0.35,
                )
            end_seconds = min(section_end, allowed_end)
            start_seconds = max(section_start, allowed_start, end_seconds - display_seconds)
        else:
            allowed_start = (
                _seconds(topic_window.get("start"), "topic_start")
                if isinstance(topic_window, Mapping)
                else section_start
            )
            allowed_end = (
                _seconds(topic_window.get("end"), "topic_end")
                if isinstance(topic_window, Mapping)
                else section_end
            )
            start_seconds = max(
                allowed_start,
                section_start + min(1.2, max(0.0, (section_end - section_start) * 0.22)),
            )
            end_seconds = min(section_end, allowed_end, start_seconds + display_seconds)

        if fmt == "film" and start_seconds - previous_end < FILM_MIN_GAP_SECONDS:
            continue
        if end_seconds - start_seconds >= 1.5:
            events.append(
                {
                    "start": round(start_seconds, 3),
                    "end": round(end_seconds, 3),
                    "text": text,
                    "role": role,
                    "format": fmt,
                }
            )
            previous_end = end_seconds
            used_texts.add(_norm(text))
    return events[:max_events]

def _event_font_size(role: str, *, fmt: str) -> int:
    role = str(role or "turn").strip().lower()
    if fmt == "podcast":
        if role == "hook":
            return HOOK_FONT_SIZE
        if role == "payoff":
            return PAYOFF_FONT_SIZE
        return FONT_SIZE
    if role == "hook":
        return FILM_HOOK_FONT_SIZE
    if role == "payoff":
        return FILM_PAYOFF_FONT_SIZE
    return FILM_FONT_SIZE


def build_ass(events: Sequence[Mapping[str, object]], *, fmt: str = "podcast") -> str:
    if fmt not in {"podcast", "film"}:
        raise PodcastKeyTextError(f"sparse_key_text_format_invalid:{fmt}")
    font_size = FONT_SIZE if fmt == "podcast" else FILM_FONT_SIZE
    text_y = TEXT_Y if fmt == "podcast" else FILM_TEXT_Y
    max_events = MAX_EVENTS if fmt == "podcast" else FILM_MAX_EVENTS

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
        f"Style: Caption,{BODY_FONT},{font_size},{PRIMARY_ASS},{PRIMARY_ASS},{OUTLINE_ASS},&H00000000,-1,0,0,0,100,100,0,0,1,2,0,5,100,100,0,-1",
        "",
        "[Events]",
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
    ]
    from clean_v2.podcast_captions import (
        BAR_ALPHA_ASS, BAR_COLOUR_ASS, GOLD_ASS, _rounded_card,
    )

    # Pull-quote card: the same navy + gold identity as the dialogue captions, sized to
    # the line (never a full-width band), with a gold accent on the reading side.
    lines.insert(
        lines.index("[Events]") - 1,
        f"Style: Quote,{BODY_FONT},{font_size},{PRIMARY_ASS},{PRIMARY_ASS},{OUTLINE_ASS},&H00000000,-1,0,0,0,100,100,0,0,1,2,0,5,100,100,0,-1",
    )
    for item in events[:max_events]:
        text = _clean(item.get("text"))
        if not text or _contains_prayer_text(text):
            continue
        start = _ass_time(_seconds(item.get("start"), "start"))
        end = _ass_time(_seconds(item.get("end"), "end"))
        event_size = _event_font_size(str(item.get("role") or "turn"), fmt=fmt)
        rows, event_size = _caption_rows_and_size(text, fmt=fmt, font_size=event_size)
        plain = r"\N".join(_rtl_row(row.split()) for row in rows)
        widest = max(_caption_width(row, event_size) for row in rows)
        row_height = int(event_size * 1.28)
        half_w = min(840, int(widest / 2) + 96)
        half_h = (len(rows) * row_height) // 2 + 30
        left, right = TEXT_X - half_w, TEXT_X + half_w
        top, bottom = text_y - half_h, text_y + half_h
        fade = r"\fad(220,260)"
        shape = lambda colour, alpha, extra="": (  # noqa: E731
            f"{{\\an7\\pos(0,0){fade}\\1c{colour}\\1a{alpha}\\bord0\\shad0{extra}\\p1}}"
        )
        lines.append(
            f"Dialogue: 0,{start},{end},Quote,shade,0,0,0,,"
            f"{shape('&H000000&', '&H78&', chr(92) + 'blur16')}{_rounded_card(left, top + 10, right, bottom + 10, 38)}{{\\p0}}"
        )
        lines.append(
            f"Dialogue: 1,{start},{end},Quote,card,0,0,0,,"
            f"{shape(BAR_COLOUR_ASS, BAR_ALPHA_ASS)}{_rounded_card(left, top, right, bottom, 38)}{{\\p0}}"
        )
        lines.append(
            f"Dialogue: 2,{start},{end},Quote,rim,0,0,0,,"
            f"{{\\an7\\pos(0,0){fade}\\1a&HFF&\\3c{GOLD_ASS}\\3a&H60&\\bord1.5\\shad0\\p1}}"
            f"{_rounded_card(left, top, right, bottom, 38)}{{\\p0}}"
        )
        lines.append(
            f"Dialogue: 2,{start},{end},Quote,accent,0,0,0,,"
            f"{shape(GOLD_ASS, '&H00&')}{_rounded_card(right - 44, top + 30, right - 36, bottom - 30, 4)}{{\\p0}}"
        )
        lines.append(
            f"Dialogue: 3,{start},{end},Quote,line,0,0,0,,"
            f"{{\\an5{fade}\\pos({TEXT_X - 14},{text_y})\\fs{event_size}\\bord2\\blur0.6\\c{PRIMARY_ASS}}}{plain}"
        )
    lines.append("")
    return "\n".join(lines)


def _apply_sparse_key_text(
    *,
    output_dir: Path,
    final_path: Path,
    script: Mapping[str, Any],
    fmt: str,
) -> dict[str, Any]:
    timeline_path = Path(output_dir) / "timeline-first.json"
    identity_path = Path(output_dir) / "narrative-identity.json"
    try:
        timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PodcastKeyTextError("podcast_key_text_timeline_missing") from exc

    closer = ""
    if identity_path.is_file():
        try:
            identity = json.loads(identity_path.read_text(encoding="utf-8"))
            closer = str(identity.get("closer") or "") if isinstance(identity, dict) else ""
        except (OSError, json.JSONDecodeError):
            closer = ""

    # On-screen Arabic is owned by the accepted final narration, never by
    # planner-authored display_text_ar. This keeps Film/Podcast copy inside the
    # same Arabic/tone audit that approved the spoken text.
    try:
        from clean_v2.podcast_captions import build_events as build_speech_cues

        cues, _cue_mode = build_speech_cues(Path(output_dir), fmt="podcast")
    except Exception as exc:  # no measured speech -> no key text; a line shown off-time is worse than none
        raise PodcastKeyTextError(
            f"{fmt}_key_text_speech_timing_unavailable:{type(exc).__name__}"
        ) from exc
    events = build_events(script=script, timeline=timeline, closer=closer, fmt=fmt, cues=cues)
    from clean_v2.board_scene import blocked_windows, drop_events_over_boards

    boards = blocked_windows(Path(output_dir))
    if boards:  # an explainer board carries its own text; never write over it
        events = drop_events_over_boards(events, boards)
    narration = " ".join(
        str(item.get("narration") or "") for item in (script.get("sections") or []) if isinstance(item, Mapping)
    )
    for event in events:
        if not occurs_in_source(event.get("text"), narration):
            raise PodcastKeyTextError("podcast_key_text_text_not_in_approved_script")
    max_events = MAX_EVENTS if fmt == "podcast" else FILM_MAX_EVENTS
    font_size = FONT_SIZE if fmt == "podcast" else FILM_FONT_SIZE
    if not events:
        return {
            "schema_version": SCHEMA_VERSION,
            "renderer_version": RENDERER_VERSION,
            "status": "skipped_no_compact_key_lines",
            "format": fmt,
            "event_count": 0,
            "max_events": max_events,
            "provider_calls_added": 0,
            "black_text_box": False,
        }

    ass_path = Path(output_dir) / f"{fmt}-key-text.ass"
    rendered = Path(output_dir) / f".final-{fmt}-key-text.mp4"
    try:
        ass_path.write_text(build_ass(events, fmt=fmt), encoding="utf-8")
        completed = subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "verbose",
                "-i", str(Path(final_path).resolve()),
                "-vf", f"ass='{_filter_escape_path(ass_path)}':shaping=complex",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "copy", str(rendered.resolve()),
            ],
            check=True,
            timeout=1800,
            env=_secret_free_subprocess_env(),
            capture_output=True,
            text=True,
        )
        font_used = resolved_font(completed.stderr, BODY_FONT)
        if not rendered.is_file() or rendered.stat().st_size <= 0:
            raise PodcastKeyTextError("podcast_key_text_render_missing_or_empty")
        os.replace(rendered, Path(final_path))
    except PodcastKeyTextError:
        rendered.unlink(missing_ok=True)
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        rendered.unlink(missing_ok=True)
        raise PodcastKeyTextError(
            f"{fmt}_key_text_local_render_failed:{type(exc).__name__}"
        ) from exc
    finally:
        ass_path.unlink(missing_ok=True)
    return {
        "schema_version": SCHEMA_VERSION,
        "renderer": "ffmpeg_libass_sparse_key_text_cairo_bold",
        "renderer_version": RENDERER_VERSION,
        "status": "pass",
        "format": fmt,
        "event_count": len(events),
        "max_events": max_events,
        "events": events,
        "accent_rgb": None,
        "accent_enabled": False,
        "body_rgb": "#F4F2EE",
        "font": BODY_FONT,
        "font_actually_used": font_used,
        "font_size": font_size,
        "hook_font_size": _event_font_size("hook", fmt=fmt),
        "payoff_font_size": _event_font_size("payoff", fmt=fmt),
        "max_caption_lines": 2,
        "depth_layers": 1,
        "black_text_box": False,
        "font_weight": "bold",
        "font_family_contract": "Noto Sans Arabic Bold",
        "outline_px": 4,
        "shadow_offset": [0, 0],
        "extrusion_offset": [0, 0],
        "motion": "static_phrase_fade_180_240ms",
        "provider_calls_added": 0,
        "style_source": "shared_cairo_bold_offwhite_black_outline",
        "text_source_policy": "final_audited_script_sentence_only",
        "rtl_policy": "full_phrase_static_offwhite_cairo_bold_no_directional_word_sweep",
        "burned_in_policy": "listener_question_plus_sparse_key_lines_not_full_transcript",
        "speaker_labels_visible": False,
    }


def apply_podcast_key_text(
    *,
    output_dir: Path,
    final_path: Path,
    script: Mapping[str, Any],
) -> dict[str, Any]:
    return _apply_sparse_key_text(
        output_dir=output_dir,
        final_path=final_path,
        script=script,
        fmt="podcast",
    )


def apply_film_key_text(
    *,
    output_dir: Path,
    final_path: Path,
    script: Mapping[str, Any],
) -> dict[str, Any]:
    return _apply_sparse_key_text(
        output_dir=output_dir,
        final_path=final_path,
        script=script,
        fmt="film",
    )

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
    _plain_caption,
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
    return " ".join(str(value or "").replace("\n", " ").split()).strip()


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
) -> list[dict[str, object]]:
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

        section_start = _seconds(timing.get("start"), "start")
        section_end = _seconds(timing.get("end"), "end")
        if section_end <= section_start:
            raise PodcastKeyTextError("podcast_key_text_duration_invalid")

        if fmt == "podcast" and role == "hook" and isinstance(hook_window, Mapping):
            start_seconds = _seconds(hook_window.get("start"), "hook_start")
            end_seconds = min(_seconds(hook_window.get("end"), "hook_end"), start_seconds + display_seconds)
        elif role == "payoff" and (
            isinstance(pre_outro_window, Mapping) or isinstance(outro_window, Mapping)
        ):
            boundary = (
                _seconds(pre_outro_window.get("start"), "pre_outro_start")
                if isinstance(pre_outro_window, Mapping)
                else _seconds(outro_window.get("start"), "outro_start") - 0.35
            )
            end_seconds = max(section_start, min(section_end, boundary))
            start_seconds = max(section_start, end_seconds - display_seconds)
        else:
            start_seconds = section_start + min(1.2, max(0.0, (section_end - section_start) * 0.22))
            if fmt == "film" and index == 0 and isinstance(topic_window, Mapping):
                start_seconds = max(start_seconds, _seconds(topic_window.get("start"), "topic_start") + 0.8)
            end_seconds = min(section_end, start_seconds + display_seconds)

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
    return events[:max_events]

def _visual_beat_text_events(
    *,
    output_dir: Path,
    timeline: Mapping[str, Any],
    fmt: str,
) -> list[dict[str, object]]:
    """Sparse format-aware text selected from the same visual beats on screen."""
    manifest_path = Path(output_dir) / "rights-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    assets = manifest.get("assets") if isinstance(manifest, Mapping) else None
    section_events = timeline.get("section_events")
    if not isinstance(assets, list) or not isinstance(section_events, list):
        return []

    by_section: dict[str, list[Mapping[str, Any]]] = {}
    for row in assets:
        if not isinstance(row, Mapping):
            continue
        section_id = str(row.get("section_id") or "").strip()
        display = _clean(row.get("display_text_ar"))
        beat_id = str(row.get("beat_id") or "").strip()
        if section_id and display and beat_id:
            by_section.setdefault(section_id, []).append(row)

    all_events: list[dict[str, object]] = []
    for timing in section_events:
        if not isinstance(timing, Mapping):
            return []
        section_id = str(timing.get("section_id") or "").strip()
        rows = by_section.get(section_id) or []
        if not rows:
            continue
        section_start = _seconds(timing.get("start"), "visual_start")
        section_end = _seconds(timing.get("end"), "visual_end")
        if section_end <= section_start:
            continue
        slot = (section_end - section_start) / len(rows)
        for row_index, row in enumerate(rows):
            beat_start = section_start + slot * row_index
            beat_end = section_end if row_index == len(rows) - 1 else section_start + slot * (row_index + 1)
            raw_role = str(row.get("role") or "").strip()
            role = "hook" if raw_role == "hook" else ("payoff" if raw_role == "payoff" else "turn")
            allowed_kind = "hook" if role == "hook" else "topic"
            allowed = _identity_text_window(timeline, kind=allowed_kind)
            if allowed is None:
                return []
            beat_start = max(beat_start, allowed[0])
            beat_end = min(beat_end, allowed[1])
            if beat_end <= beat_start:
                continue
            all_events.append(
                {
                    "start": round(beat_start, 3),
                    "end": round(beat_end, 3),
                    "text": _clean(row.get("display_text_ar")),
                    "role": role,
                    "format": fmt,
                    "beat_id": str(row.get("beat_id") or ""),
                    "text_source": "visual_beat_display_text_ar",
                }
            )
    if not all_events:
        return []

    if fmt == "podcast":
        if len(all_events) <= MAX_EVENTS:
            selected_indices = list(range(len(all_events)))
        else:
            selected_indices = sorted({0, len(all_events) // 2, len(all_events) - 1})
        display_seconds = DISPLAY_SECONDS
    else:
        total_seconds = _seconds(section_events[-1].get("end"), "visual_total_end")
        selected_indices = _film_selected_indices(len(all_events), total_seconds)
        display_seconds = FILM_DISPLAY_SECONDS

    selected: list[dict[str, object]] = []
    previous_end = -999.0
    for index in selected_indices:
        item = dict(all_events[index])
        beat_start = float(item["start"])
        beat_end = float(item["end"])
        start = beat_start + min(0.6, max(0.0, (beat_end - beat_start) * 0.12))
        end = min(beat_end, start + display_seconds)
        if fmt == "film" and start - previous_end < FILM_MIN_GAP_SECONDS:
            continue
        if end - start < 1.5:
            continue
        item["start"] = round(start, 3)
        item["end"] = round(end, 3)
        selected.append(item)
        previous_end = end
    if selected:
        selected[0]["role"] = "hook"
        selected[-1]["role"] = "payoff"
    return selected


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
        f"Style: Caption,{BODY_FONT},{font_size},{PRIMARY_ASS},{PRIMARY_ASS},{OUTLINE_ASS},&H00000000,-1,0,0,0,100,100,0,0,1,4,0,5,100,100,0,1",
        "",
        "[Events]",
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
    ]
    common = r"\an5\fad(180,240)"
    for item in events[:max_events]:
        text = _clean(item.get("text"))
        if not text or _contains_prayer_text(text):
            continue
        start = _ass_time(_seconds(item.get("start"), "start"))
        end = _ass_time(_seconds(item.get("end"), "end"))
        plain = _plain_caption(text)
        event_size = _event_font_size(str(item.get("role") or "turn"), fmt=fmt)
        lines.append(
            f"Dialogue: 0,{start},{end},Caption,,0,0,0,,"
            f"{{{common}\\pos({TEXT_X},{text_y})\\fs{event_size}\\c{PRIMARY_ASS}}}{plain}"
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
    events = build_events(script=script, timeline=timeline, closer=closer, fmt=fmt)
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
        subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(Path(final_path).resolve()),
                "-vf", f"subtitles='{_filter_escape_path(ass_path)}'",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "copy", str(rendered.resolve()),
            ],
            check=True,
            timeout=1800,
            env=_secret_free_subprocess_env(),
        )
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
        "font_size": font_size,
        "hook_font_size": _event_font_size("hook", fmt=fmt),
        "payoff_font_size": _event_font_size("payoff", fmt=fmt),
        "max_caption_lines": 2,
        "depth_layers": 1,
        "black_text_box": False,
        "font_weight": "bold",
        "font_family_contract": "Cairo Bold",
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

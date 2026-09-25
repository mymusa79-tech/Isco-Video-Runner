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
RENDERER_VERSION = "clean-v2-sparse-key-text-3d-lite-v2"
MAX_EVENTS = 3
FONT_SIZE = min(82, BODY_FONT_SIZE)
TEXT_X = 960
TEXT_Y = 770
EXTRUDE = (2, 3)
SHADOW = (4, 5)
DISPLAY_SECONDS = 4.2
MAX_WORDS = 9

FILM_MAX_EVENTS = 5
FILM_FONT_SIZE = min(108, BODY_FONT_SIZE)
FILM_TEXT_Y = 760
FILM_DISPLAY_SECONDS = 5.0
FILM_MAX_WORDS = 7
FILM_MIN_GAP_SECONDS = 12.0
TRANSITION_MARKERS = ("لكن", "المشكلة", "الحقيقة", "ربما", "وهنا", "لأن", "لهذا")


class PodcastKeyTextError(RuntimeError):
    pass


def _clean(value: object) -> str:
    return " ".join(str(value or "").replace("\n", " ").split()).strip()


def _sentences(value: object) -> list[str]:
    text = _clean(value)
    if not text:
        return []
    return [part.strip() for part in re.split(r"(?<=[.!؟!])\s+", text) if part.strip()] or [text]


def _compact_candidates(value: object, *, max_words: int = MAX_WORDS) -> list[str]:
    candidates: list[str] = []
    for sentence in _sentences(value):
        pieces = [sentence]
        if "،" in sentence:
            pieces = [part.strip() for part in sentence.split("،") if part.strip()]
        for piece in pieces:
            if 3 <= len(piece.split()) <= max_words:
                candidates.append(piece)
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
    candidates = _compact_candidates(text, max_words=max_words)
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
        elif role == "payoff" and isinstance(outro_window, Mapping):
            end_seconds = max(section_start, _seconds(outro_window.get("start"), "outro_start") - 0.35)
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
        f"Style: Shadow,{BODY_FONT},{font_size},{SHADOW_ASS},{SHADOW_ASS},{SHADOW_ASS},{SHADOW_ASS},-1,0,0,0,100,100,0,0,1,0,0,5,100,100,0,1",
        f"Style: Extrusion,{BODY_FONT},{font_size},{EXTRUSION_ASS},{EXTRUSION_ASS},{OUTLINE_ASS},&H00000000,-1,0,0,0,100,100,0,0,1,2,0,5,100,100,0,1",
        f"Style: Caption,{BODY_FONT},{font_size},{PRIMARY_ASS},{PRIMARY_ASS},{OUTLINE_ASS},&H00000000,-1,0,0,0,100,100,0,0,1,3,0,5,100,100,0,1",
        "",
        "[Events]",
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
    ]
    common = r"\an5\fad(180,240)\fscx99\fscy99\t(0,140,\fscx100\fscy100)"
    for item in events[:max_events]:
        text = _clean(item.get("text"))
        if not text:
            continue
        start = _ass_time(_seconds(item.get("start"), "start"))
        end = _ass_time(_seconds(item.get("end"), "end"))
        plain = _plain_caption(text)
        face = _accent_caption(text, _accent_word_index(text))
        lines.extend(
            [
                f"Dialogue: 0,{start},{end},Shadow,,0,0,0,,{{{common}\\pos({TEXT_X + SHADOW[0]},{text_y + SHADOW[1]})}}{plain}",
                f"Dialogue: 1,{start},{end},Extrusion,,0,0,0,,{{{common}\\pos({TEXT_X + EXTRUDE[0]},{text_y + EXTRUDE[1]})}}{plain}",
                f"Dialogue: 2,{start},{end},Caption,,0,0,0,,{{{common}\\pos({TEXT_X},{text_y})}}{face}",
            ]
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
        "renderer": "ffmpeg_libass_sparse_key_text_3d_lite",
        "renderer_version": RENDERER_VERSION,
        "status": "pass",
        "format": fmt,
        "event_count": len(events),
        "max_events": max_events,
        "events": events,
        "accent_rgb": "#D7A85B",
        "body_rgb": "#FFFFFF",
        "font": BODY_FONT,
        "font_size": font_size,
        "depth_layers": 3,
        "black_text_box": False,
        "shadow_offset": list(SHADOW),
        "extrusion_offset": list(EXTRUDE),
        "motion": "fade_180_240ms_scale_99_to_100",
        "provider_calls_added": 0,
        "style_source": "shared_sparse_3d_key_text",
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

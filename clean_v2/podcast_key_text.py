from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from clean_v2.short_timed_text import (
    ACCENT_ASS,
    BODY_FONT,
    EXTRUSION_ASS,
    OUTLINE_ASS,
    PRIMARY_ASS,
    SHADOW_ASS,
    _filter_escape_path,
    _secret_free_subprocess_env,
)

SCHEMA_VERSION = 1
RENDERER_VERSION = "clean-v2-podcast-key-text-3d-lite-v1"
MAX_EVENTS = 3
FONT_SIZE = 78
TEXT_X = 960
TEXT_Y = 770
EXTRUDE_X = 4
EXTRUDE_Y = 5
SHADOW_X = 9
SHADOW_Y = 11
DISPLAY_SECONDS = 4.2
FINAL_QUIET_SECONDS = 1.2
MAX_WORDS = 14
TRANSITION_MARKERS = ("لكن", "المشكلة", "الحقيقة", "ربما", "وهنا", "لأن", "لهذا")
_STOPWORDS = frozenset({
    "في", "من", "على", "إلى", "عن", "مع", "أن", "إن", "ثم", "أو", "بل",
    "لكن", "هذا", "هذه", "ذلك", "التي", "الذي", "هو", "هي", "كان", "كنت",
    "ما", "لا", "لم", "لن", "قد", "كل", "حتى", "فقط",
})


class PodcastKeyTextError(RuntimeError):
    pass


@dataclass(frozen=True)
class KeyTextEvent:
    start: float
    end: float
    text: str
    role: str


def _clean(value: object) -> str:
    return " ".join(str(value or "").replace("\n", " ").split()).strip()


def _sentences(value: object) -> list[str]:
    text = _clean(value)
    if not text:
        return []
    return [
        part.strip()
        for part in re.split(r"(?<=[.!؟!])\s+", text)
        if part.strip()
    ] or [text]


def _usable(sentence: str) -> bool:
    count = len(_clean(sentence).split())
    return 3 <= count <= MAX_WORDS


def _pick_text(narration: str, role: str, *, closer: str = "") -> str:
    text = _clean(narration)
    normalized_closer = _clean(closer)
    if normalized_closer and text.endswith(normalized_closer):
        text = text[: -len(normalized_closer)].strip()
    sentences = _sentences(text)
    if not sentences:
        return ""

    if role == "hook":
        return sentences[0] if _usable(sentences[0]) else ""

    if role == "turn":
        for sentence in sentences:
            if _usable(sentence) and any(marker in sentence for marker in TRANSITION_MARKERS):
                return sentence
        candidates = [sentence for sentence in sentences if _usable(sentence)]
        return candidates[0] if candidates else ""

    candidates = [sentence for sentence in sentences if _usable(sentence)]
    return candidates[-1] if candidates else ""


def _seconds(value: object, label: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise PodcastKeyTextError(f"podcast_key_text_{label}_invalid") from None
    if parsed < 0:
        raise PodcastKeyTextError(f"podcast_key_text_{label}_invalid")
    return parsed


def build_events(
    *,
    script: Mapping[str, Any],
    timeline: Mapping[str, Any],
    closer: str = "",
) -> list[dict[str, object]]:
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

    selected = [(0, "hook"), (len(sections) // 2, "turn"), (len(sections) - 1, "payoff")]
    deduped: list[tuple[int, str]] = []
    seen: set[int] = set()
    for index, role in selected:
        if index not in seen:
            seen.add(index)
            deduped.append((index, role))

    events: list[dict[str, object]] = []
    for index, role in deduped[:MAX_EVENTS]:
        section = sections[index]
        timing = section_events[index]
        if not isinstance(section, Mapping) or not isinstance(timing, Mapping):
            raise PodcastKeyTextError("podcast_key_text_section_invalid")
        expected_id = str(section.get("id") or "")
        if str(timing.get("section_id") or "") != expected_id:
            raise PodcastKeyTextError("podcast_key_text_section_order_invalid")

        text = _pick_text(
            str(section.get("narration") or ""),
            role,
            closer=closer if role == "payoff" else "",
        )
        if not text:
            continue

        section_start = _seconds(timing.get("start"), "start")
        section_end = _seconds(timing.get("end"), "end")
        if section_end <= section_start:
            raise PodcastKeyTextError("podcast_key_text_duration_invalid")

        if role == "hook":
            start = section_start + min(0.15, max(0.0, (section_end - section_start) * 0.05))
            end = min(section_end, start + DISPLAY_SECONDS)
        elif role == "turn":
            start = section_start + min(0.8, max(0.0, (section_end - section_start) * 0.18))
            end = min(section_end, start + DISPLAY_SECONDS)
        else:
            end = max(section_start, section_end - FINAL_QUIET_SECONDS)
            start = max(section_start, end - DISPLAY_SECONDS)

        if end - start < 1.5:
            continue
        events.append(
            {
                "start": round(start, 3),
                "end": round(end, 3),
                "text": text,
                "role": role,
            }
        )
    return events


def _ass_time(seconds: float) -> str:
    centis = int(round(max(0.0, seconds) * 100))
    hours, rem = divmod(centis, 360000)
    minutes, rem = divmod(rem, 6000)
    secs, cs = divmod(rem, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{cs:02d}"


def _ass_escape(text: str) -> str:
    return _clean(text).replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")


def _wrap(text: str, maximum_words: int = 6) -> str:
    words = _clean(text).split()
    lines = [" ".join(words[i:i + maximum_words]) for i in range(0, len(words), maximum_words)]
    return r"\N".join(_ass_escape(line) for line in lines)


def _focus_index(text: str) -> int:
    words = _clean(text).split()
    candidates: list[tuple[int, int]] = []
    for index, word in enumerate(words):
        bare = re.sub(r"[^\w\u0600-\u06FF]+", "", word, flags=re.UNICODE)
        if bare and bare not in _STOPWORDS:
            candidates.append((len(bare), index))
    return max(candidates)[1] if candidates else max(0, len(words) - 1)


def _face_text(text: str) -> str:
    words = _clean(text).split()
    focus = _focus_index(text)
    rendered: list[str] = []
    for index, word in enumerate(words):
        escaped = _ass_escape(word)
        if index == focus:
            rendered.append("{\\c" + ACCENT_ASS + "}" + escaped + "{\\c" + PRIMARY_ASS + "}")
        else:
            rendered.append(escaped)
    # Re-wrap after color tags by preserving authored words in compact lines.
    if len(words) <= 6:
        return "\u202B" + " ".join(rendered) + "\u202C"
    chunks = [" ".join(rendered[i:i + 6]) for i in range(0, len(rendered), 6)]
    return "\u202B" + r"\N".join(chunks) + "\u202C"


def build_ass(events: Sequence[Mapping[str, object]]) -> str:
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
        f"Style: Shadow,{BODY_FONT},{FONT_SIZE},{SHADOW_ASS},{SHADOW_ASS},{SHADOW_ASS},{SHADOW_ASS},-1,0,0,0,100,100,0,0,1,0,0,5,100,100,0,1",
        f"Style: Extrusion,{BODY_FONT},{FONT_SIZE},{EXTRUSION_ASS},{EXTRUSION_ASS},{OUTLINE_ASS},&H00000000,-1,0,0,0,100,100,0,0,1,3,0,5,100,100,0,1",
        f"Style: Caption,{BODY_FONT},{FONT_SIZE},{PRIMARY_ASS},{PRIMARY_ASS},{OUTLINE_ASS},&H00000000,-1,0,0,0,100,100,0,0,1,5,0,5,100,100,0,1",
        "",
        "[Events]",
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
    ]
    for raw in events[:MAX_EVENTS]:
        start = _ass_time(_seconds(raw.get("start"), "start"))
        end = _ass_time(_seconds(raw.get("end"), "end"))
        text = _clean(raw.get("text"))
        if not text:
            continue
        plain = "\u202B" + _wrap(text) + "\u202C"
        face = _face_text(text)
        common = r"\an5\fad(180,240)\fscx98\fscy98\t(0,120,\fscx100\fscy100)"
        lines.append(
            f"Dialogue: 0,{start},{end},Shadow,,0,0,0,,"
            f"{{{common}\\pos({TEXT_X + SHADOW_X},{TEXT_Y + SHADOW_Y})}}{plain}"
        )
        lines.append(
            f"Dialogue: 1,{start},{end},Extrusion,,0,0,0,,"
            f"{{{common}\\pos({TEXT_X + EXTRUDE_X},{TEXT_Y + EXTRUDE_Y})}}{plain}"
        )
        lines.append(
            f"Dialogue: 2,{start},{end},Caption,,0,0,0,,"
            f"{{{common}\\pos({TEXT_X},{TEXT_Y})}}{face}"
        )
    lines.append("")
    return "\n".join(lines)


def apply_podcast_key_text(
    *,
    output_dir: Path,
    final_path: Path,
    script: Mapping[str, Any],
) -> dict[str, Any]:
    timeline_path = Path(output_dir) / "timeline-first.json"
    identity_path = Path(output_dir) / "narrative-identity.json"
    try:
        timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PodcastKeyTextError("podcast_key_text_timeline_missing") from exc
    identity: Mapping[str, Any] = {}
    if identity_path.is_file():
        try:
            parsed = json.loads(identity_path.read_text(encoding="utf-8"))
            if isinstance(parsed, Mapping):
                identity = parsed
        except (OSError, json.JSONDecodeError):
            identity = {}

    events = build_events(
        script=script,
        timeline=timeline,
        closer=str(identity.get("closer") or ""),
    )
    if not events:
        return {
            "schema_version": SCHEMA_VERSION,
            "renderer_version": RENDERER_VERSION,
            "status": "skipped_no_compact_key_lines",
            "event_count": 0,
            "max_events": MAX_EVENTS,
            "provider_calls_added": 0,
            "black_text_box": False,
        }

    ass_path = Path(output_dir) / "podcast-key-text.ass"
    ass_path.write_text(build_ass(events), encoding="utf-8")
    rendered = Path(output_dir) / ".final-podcast-key-text.mp4"
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(Path(final_path).resolve()),
                "-vf",
                f"subtitles='{_filter_escape_path(ass_path)}'",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "20",
                "-c:a",
                "copy",
                str(rendered.resolve()),
            ],
            check=True,
            timeout=1800,
            env=_secret_free_subprocess_env(),
        )
    finally:
        ass_path.unlink(missing_ok=True)

    if not rendered.is_file() or rendered.stat().st_size <= 0:
        raise PodcastKeyTextError("podcast_key_text_render_missing_or_empty")
    os.replace(rendered, Path(final_path))
    return {
        "schema_version": SCHEMA_VERSION,
        "renderer": "ffmpeg_libass_podcast_3d_lite",
        "renderer_version": RENDERER_VERSION,
        "status": "pass",
        "event_count": len(events),
        "max_events": MAX_EVENTS,
        "events": events,
        "accent_rgb": "#D7A85B",
        "body_rgb": "#FFFFFF",
        "font": BODY_FONT,
        "font_size": FONT_SIZE,
        "depth_layers": 3,
        "black_text_box": False,
        "extrusion_offset": [EXTRUDE_X, EXTRUDE_Y],
        "shadow_offset": [SHADOW_X, SHADOW_Y],
        "provider_calls_added": 0,
        "style_source": "approved_short_3d_tracked_text",
    }

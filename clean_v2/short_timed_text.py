from __future__ import annotations

import json
import math
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .media import probe_duration

SCHEMA_VERSION = 4
RICH_RENDERER_VERSION = "clean-v2-short-cinematic-type-v5"
ALLOWED_ROLES = {"hook", "beat", "payoff"}

# Cinematic Type: keep one dependable Arabic family, but give the face a
# warm-gold accent, crisp outline, real offset depth and a separate shadow
# layer. No black rectangle is ever introduced behind the text.
ACCENT_ASS = "&H005BA8D7"  # RGB #D7A85B warm channel gold
PRIMARY_ASS = "&H00FFFFFF"  # RGB #FFFFFF
OUTLINE_ASS = "&H00000000"  # opaque black
EXTRUSION_ASS = "&H00231A12"  # dark warm side face
SHADOW_ASS = "&H76000000"  # semi-transparent black
BODY_FONT = "Noto Sans Arabic"
FOCUS_FONT = BODY_FONT
BODY_FONT_SIZE = 104
FOCUS_FONT_SIZE = 136
BODY_WRAP_WORDS = 5
CAPTION_MIN_WORDS = 2
CAPTION_MAX_WORDS = 5
CAPTION_RIGHT_X = 980
CAPTION_BODY_Y = 650
CAPTION_FOCUS_Y = 800
CAPTION_Y = CAPTION_BODY_Y  # compatibility/report alias
CAPTION_EXTRUDE_X = 4
CAPTION_EXTRUDE_Y = 5
CAPTION_SHADOW_X = 9
CAPTION_SHADOW_Y = 11
MAX_DARK_SLATES = 0
TRANSITION_MARKERS = ("لكن", "الحقيقة", "المشكلة", "الآن", "ابدأ")

_SECRET_ENV_NAMES = {
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "OPENROUTER_API_KEY",
    "MISTRAL_API_KEY",
    "PEXELS_API_KEY",
    "PIXABAY_API_KEY",
    "CLOUDFLARE_API_TOKEN",
    "AZURE_SPEECH_KEY",
    "YOUTUBE_API_KEY",
    "YOUTUBE_CLIENT_SECRET",
    "YOUTUBE_REFRESH_TOKEN",
    "TELEGRAM_BOT_TOKEN",
}


class ShortTimedTextError(ValueError):
    pass


@dataclass(frozen=True)
class TimedTextEvent:
    start: float
    end: float
    text: str
    role: str


def _secret_free_subprocess_env() -> dict[str, str]:
    child_env = os.environ.copy()
    for name in list(child_env):
        upper = name.upper()
        if name in _SECRET_ENV_NAMES or upper.endswith("_API_KEY") or upper.endswith("_TOKEN"):
            child_env.pop(name, None)
    return child_env


def _seconds(value: object, field: str) -> float:
    if isinstance(value, bool):
        raise ShortTimedTextError(f"{field}_invalid")
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise ShortTimedTextError(f"{field}_invalid") from None
    if not math.isfinite(parsed) or parsed < 0:
        raise ShortTimedTextError(f"{field}_invalid")
    return parsed


def _clean(value: object) -> str:
    return " ".join(str(value or "").replace("\n", " ").split()).strip()


def _sentences(text: object) -> list[str]:
    compact = _clean(text)
    if not compact:
        return []
    return [
        item.strip()
        for item in re.split(r"(?<=[.!؟!])\s+", compact)
        if item.strip()
    ] or [compact]


def _phrase_chunks(text: object) -> list[str]:
    """Split authored narration into balanced 2-5 word caption phrases."""
    chunks: list[str] = []
    for sentence in _sentences(text):
        words = sentence.split()
        if not words:
            continue
        if len(words) <= CAPTION_MAX_WORDS:
            chunks.append(" ".join(words))
            continue
        chunk_count = math.ceil(len(words) / CAPTION_MAX_WORDS)
        base, extra = divmod(len(words), chunk_count)
        cursor = 0
        for index in range(chunk_count):
            size = base + (1 if index < extra else 0)
            piece = words[cursor : cursor + size]
            cursor += size
            if piece:
                chunks.append(" ".join(piece))

    # Avoid one-word flashes when punctuation created a tiny standalone
    # sentence. Merge locally when a neighbor still stays within five words.
    index = 0
    while len(chunks) > 1 and index < len(chunks):
        if len(chunks[index].split()) >= CAPTION_MIN_WORDS:
            index += 1
            continue
        if index > 0 and len(chunks[index - 1].split()) < CAPTION_MAX_WORDS:
            chunks[index - 1] = f"{chunks[index - 1]} {chunks[index]}"
            chunks.pop(index)
            continue
        if index + 1 < len(chunks) and len(chunks[index + 1].split()) < CAPTION_MAX_WORDS:
            chunks[index] = f"{chunks[index]} {chunks[index + 1]}"
            chunks.pop(index + 1)
            index += 1
            continue
        index += 1
    return chunks


def _section_phrase_events(
    narration: object,
    *,
    section_index: int,
    start: float,
    end: float,
) -> list[dict[str, object]]:
    chunks = _phrase_chunks(narration)
    if not chunks:
        raise ShortTimedTextError("timed_text_section_narration_missing")
    duration = end - start
    if duration <= 0:
        raise ShortTimedTextError("timed_text_duration_invalid")
    weights = [max(1, len(chunk.split())) for chunk in chunks]
    total_weight = sum(weights)
    cursor = start
    events: list[dict[str, object]] = []
    for index, (chunk, weight) in enumerate(zip(chunks, weights)):
        chunk_start = cursor
        chunk_end = (
            end
            if index == len(chunks) - 1
            else cursor + duration * (weight / total_weight)
        )
        if section_index == 0 and index == 0:
            role = "hook"
        elif section_index == 2 and index == len(chunks) - 1:
            role = "payoff"
        else:
            role = "beat"
        events.append(
            {
                "start": round(chunk_start, 3),
                "end": round(chunk_end, 3),
                "text": chunk,
                "role": role,
            }
        )
        cursor = chunk_end
    return events


def validate_progressive_text(events: Sequence[Mapping[str, object]]) -> tuple[TimedTextEvent, ...]:
    if not isinstance(events, Sequence) or isinstance(events, (str, bytes)) or not events:
        raise ShortTimedTextError("timed_text_events_missing_or_malformed")
    normalized: list[TimedTextEvent] = []
    previous_end = 0.0
    for index, raw in enumerate(events):
        if not isinstance(raw, Mapping):
            raise ShortTimedTextError("timed_text_event_malformed")
        start = _seconds(raw.get("start"), "start")
        end = _seconds(raw.get("end"), "end")
        text = _clean(raw.get("text"))
        role = str(raw.get("role") or "").strip().lower()
        if not text:
            raise ShortTimedTextError("timed_text_empty")
        if role not in ALLOWED_ROLES:
            raise ShortTimedTextError("timed_text_role_invalid")
        if end <= start:
            raise ShortTimedTextError("timed_text_duration_invalid")
        if index and start < previous_end - 0.001:
            raise ShortTimedTextError("timed_text_overlap")
        previous_end = end
        normalized.append(TimedTextEvent(start=start, end=end, text=text, role=role))
    if normalized[0].role != "hook":
        raise ShortTimedTextError("timed_text_hook_event_missing")
    if normalized[-1].role != "payoff":
        raise ShortTimedTextError("timed_text_payoff_event_missing")
    return tuple(normalized)


def _select_event_text(narration: object, role: str) -> str:
    sentences = _sentences(narration)
    if not sentences:
        raise ShortTimedTextError("timed_text_section_narration_missing")
    if role == "hook":
        return sentences[0]
    if role == "beat":
        for sentence in sentences:
            if any(marker in sentence for marker in TRANSITION_MARKERS):
                return sentence
        return sentences[0]
    return sentences[-1]


def build_events_from_section_audio(
    *,
    script: Mapping[str, Any],
    audio_dir: Path,
    mastered_narration: Path,
) -> list[dict[str, object]]:
    sections = script.get("sections") or []
    if not isinstance(sections, list) or len(sections) != 3:
        raise ShortTimedTextError("short_timed_text_requires_exactly_three_sections")

    durations: list[float] = []
    for index in range(1, 4):
        path = Path(audio_dir) / f"{index:02d}.wav"
        if not path.is_file() or path.stat().st_size <= 0:
            raise ShortTimedTextError(f"short_timed_text_section_audio_missing index={index}")
        durations.append(probe_duration(path))

    mastered_total = probe_duration(Path(mastered_narration))
    raw_total = sum(durations)
    if raw_total <= 0 or mastered_total <= 0:
        raise ShortTimedTextError("short_timed_text_audio_duration_invalid")

    # Preserve exact section boundaries, then split only the display layer into
    # short phrases. Timing remains owned by measured voice; no word alignment
    # or extra model call is claimed.
    scale = mastered_total / raw_total
    cursor = 0.0
    events: list[dict[str, object]] = []
    for section_index, (section, duration) in enumerate(zip(sections, durations)):
        if not isinstance(section, Mapping):
            raise ShortTimedTextError("short_timed_text_section_invalid")
        start = cursor
        end = mastered_total if section_index == 2 else cursor + duration * scale
        events.extend(
            _section_phrase_events(
                section.get("narration"),
                section_index=section_index,
                start=start,
                end=end,
            )
        )
        cursor = end
    validate_progressive_text(events)
    return events


def build_events_from_voice_timeline(
    *,
    script: Mapping[str, Any],
    timeline_report: Mapping[str, Any],
) -> list[dict[str, object]]:
    sections = script.get("sections") or []
    raw_events = timeline_report.get("section_events")
    if (
        timeline_report.get("status") != "pass"
        or not isinstance(sections, list)
        or len(sections) != 3
        or not isinstance(raw_events, list)
        or len(raw_events) != 3
    ):
        raise ShortTimedTextError("short_timed_text_voice_timeline_invalid")

    events: list[dict[str, object]] = []
    for section_index, (section, raw) in enumerate(zip(sections, raw_events)):
        if not isinstance(section, Mapping) or not isinstance(raw, Mapping):
            raise ShortTimedTextError("short_timed_text_voice_timeline_invalid")
        expected_id = f"s{section_index + 1}"
        if str(raw.get("section_id") or "") != expected_id:
            raise ShortTimedTextError("short_timed_text_voice_timeline_section_order_invalid")
        events.extend(
            _section_phrase_events(
                section.get("narration"),
                section_index=section_index,
                start=_seconds(raw.get("start"), "start"),
                end=_seconds(raw.get("end"), "end"),
            )
        )
    validate_progressive_text(events)
    return events


def _srt_time(seconds: float) -> str:
    millis = int(round(max(0.0, seconds) * 1000))
    hours, rem = divmod(millis, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def write_progressive_srt(events: Sequence[Mapping[str, object]], dest: Path) -> Path:
    validated = validate_progressive_text(events)
    lines: list[str] = []
    for index, item in enumerate(validated, start=1):
        lines.extend(
            [
                str(index),
                f"{_srt_time(item.start)} --> {_srt_time(item.end)}",
                item.text,
                "",
            ]
        )
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(lines), encoding="utf-8")
    return dest


def _ass_time(seconds: float) -> str:
    centis = int(round(max(0.0, seconds) * 100))
    hours, rem = divmod(centis, 360000)
    minutes, rem = divmod(rem, 6000)
    secs, cs = divmod(rem, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{cs:02d}"


def _ass_escape(text: str) -> str:
    return _clean(text).replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")


def _ass_wrap_words(text: str, *, maximum_words: int = BODY_WRAP_WORDS) -> str:
    words = _clean(text).split()
    if not words:
        return ""
    lines = [
        " ".join(words[index : index + maximum_words])
        for index in range(0, len(words), maximum_words)
    ]
    return r"\N".join(_ass_escape(line) for line in lines)


def _filter_escape_path(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")


def split_focus_phrase(text: str, role: str) -> tuple[str, str]:
    text = _clean(text)
    if not text:
        return "", ""
    for separator in (" — ", ": ", "؛ ", "، بل "):
        if separator in text:
            left, right = text.rsplit(separator, 1)
            if left.strip() and 1 <= len(right.split()) <= 5:
                return left.rstrip("،؛:- "), right.strip()

    words = text.split()
    if len(words) == 1:
        return "", text
    if len(words) == 2:
        return words[0], words[1]
    if len(words) == 3:
        return words[0], " ".join(words[1:])
    focus_size = 2 if len(words) == 4 else 3
    return " ".join(words[:-focus_size]), " ".join(words[-focus_size:])


def _slate_score(event: TimedTextEvent) -> int:
    if event.role != "beat":
        return -1
    words = event.text.split()
    if not 2 <= len(words) <= 16:
        return -1
    marker_hits = sum(1 for marker in TRANSITION_MARKERS if marker in event.text)
    if marker_hits == 0:
        return -1
    score = marker_hits * 2
    duration = event.end - event.start
    if 1.5 <= duration <= 8.0:
        score += 1
    return score


def choose_dark_slate_index(
    events: Sequence[Mapping[str, object]],
    validated: Sequence[TimedTextEvent] | None = None,
) -> int | None:
    # Caption Lite never covers footage with a dark slate.
    validate_progressive_text(events) if validated is None else tuple(validated)
    return None


def _plain_caption(text: str) -> str:
    """One shaping-safe RTL copy used for body, depth and shadow layers."""
    return "\u202B" + _ass_escape(text) + "\u202C"


def build_rich_ass(
    events: Sequence[Mapping[str, object]],
    *,
    slate_index: int | None = None,
) -> str:
    validated = validate_progressive_text(events)

    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "PlayResX: 1080",
        "PlayResY: 1920",
        "YCbCr Matrix: TV.709",
        "",
        "[V4+ Styles]",
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding",
        f"Style: Shadow,{BODY_FONT},{BODY_FONT_SIZE},{SHADOW_ASS},{SHADOW_ASS},{SHADOW_ASS},{SHADOW_ASS},-1,0,0,0,100,100,0,0,1,0,0,6,60,60,0,1",
        f"Style: Extrusion,{BODY_FONT},{BODY_FONT_SIZE},{EXTRUSION_ASS},{EXTRUSION_ASS},{OUTLINE_ASS},&H00000000,-1,0,0,0,100,100,0,0,1,4,0,6,60,60,0,1",
        f"Style: Caption,{BODY_FONT},{BODY_FONT_SIZE},{PRIMARY_ASS},{PRIMARY_ASS},{OUTLINE_ASS},&H00000000,-1,0,0,0,100,100,0,0,1,5,0,6,60,60,0,1",
        "",
        "[Events]",
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
    ]

    for item in validated:
        start = _ass_time(item.start)
        end = _ass_time(item.end)
        body, focus = split_focus_phrase(item.text, item.role)
        role_scale = 105 if item.role == "hook" else (102 if item.role == "payoff" else 100)

        def add_tier(text: str, *, y: int, size: int, colour: str, layer_base: int) -> None:
            if not text:
                return
            plain = _plain_caption(text)
            face = (
                "{\\c" + colour + f"\\fs{size}" + "}" + plain
            )
            shadow_tag = (
                rf"\an6\pos({CAPTION_RIGHT_X + CAPTION_SHADOW_X},{y + CAPTION_SHADOW_Y})"
                rf"\fscx{role_scale}\fscy{role_scale}\fs{size}"
            )
            extrusion_tag = (
                rf"\an6\pos({CAPTION_RIGHT_X + CAPTION_EXTRUDE_X},{y + CAPTION_EXTRUDE_Y})"
                rf"\fscx{role_scale}\fscy{role_scale}\fs{size}"
            )
            face_tag = (
                rf"\an6\pos({CAPTION_RIGHT_X},{y})"
                rf"\fscx{role_scale}\fscy{role_scale}\fs{size}"
            )
            lines.append(
                f"Dialogue: {layer_base},{start},{end},Shadow,,0,0,0,,"
                f"{{{shadow_tag}}}{plain}"
            )
            lines.append(
                f"Dialogue: {layer_base + 1},{start},{end},Extrusion,,0,0,0,,"
                f"{{{extrusion_tag}}}{plain}"
            )
            lines.append(
                f"Dialogue: {layer_base + 2},{start},{end},Caption,,0,0,0,,"
                f"{{{face_tag}}}{face}"
            )

        add_tier(
            body,
            y=CAPTION_BODY_Y,
            size=BODY_FONT_SIZE,
            colour=PRIMARY_ASS,
            layer_base=0,
        )
        add_tier(
            focus,
            y=CAPTION_FOCUS_Y,
            size=FOCUS_FONT_SIZE,
            colour=ACCENT_ASS,
            layer_base=3,
        )
    lines.append("")
    return "\n".join(lines)


def render_progressive_text(
    *,
    video: Path,
    events: Sequence[Mapping[str, object]],
    srt_path: Path,
    output: Path,
) -> dict[str, Any]:
    validated = validate_progressive_text(events)
    srt = write_progressive_srt(events, Path(srt_path))
    slate_index = None
    ass_path = Path(srt_path).with_suffix(".rich.ass")
    ass_path.write_text(build_rich_ass(events, slate_index=None), encoding="utf-8")

    filters: list[str] = [f"subtitles='{_filter_escape_path(ass_path)}'"]
    vf = ",".join(filters)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(Path(video).resolve()),
                "-vf",
                vf,
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "20",
                "-c:a",
                "copy",
                str(output.resolve()),
            ],
            check=True,
            timeout=180,
            env=_secret_free_subprocess_env(),
        )
    finally:
        ass_path.unlink(missing_ok=True)

    if not output.is_file() or output.stat().st_size <= 0:
        raise ShortTimedTextError("short_timed_text_render_missing_or_empty")

    return {
        "schema_version": SCHEMA_VERSION,
        "renderer": "ffmpeg_libass_cinematic_type",
        "renderer_version": RICH_RENDERER_VERSION,
        "status": "pass",
        "srt": str(srt),
        "output": str(output),
        "portrait": True,
        "event_count": len(validated),
        "dark_slate_count": 0,
        "dark_slate_index": None,
        "dark_slate_hook_forbidden": True,
        "max_dark_slates": MAX_DARK_SLATES,
        "accent_rgb": "#D7A85B",
        "body_rgb": "#FFFFFF",
        "caption_font": BODY_FONT,
        "focus_font": FOCUS_FONT,
        "body_font": BODY_FONT,
        "focus_font_size": FOCUS_FONT_SIZE,
        "body_font_size": BODY_FONT_SIZE,
        "body_wrap_words": BODY_WRAP_WORDS,
        "caption_min_words": CAPTION_MIN_WORDS,
        "caption_max_words": CAPTION_MAX_WORDS,
        "caption_y": CAPTION_Y,
        "caption_body_y": CAPTION_BODY_Y,
        "caption_focus_y": CAPTION_FOCUS_Y,
        "caption_right_x": CAPTION_RIGHT_X,
        "depth_layers": 3,
        "black_text_box": False,
        "extrusion_offset": [CAPTION_EXTRUDE_X, CAPTION_EXTRUDE_Y],
        "shadow_offset": [CAPTION_SHADOW_X, CAPTION_SHADOW_Y],
        "provider_calls": 0,
        "word_level_alignment_claimed": False,
        "word_highlight_timing": "disabled_for_static_two_tier_phrase",
        "word_highlight_count": 0,
        "two_tier_phrase_layout": True,
        "voice_owned_event_timing_preserved": True,
    }



def apply_short_timed_text(
    *,
    output_dir: Path,
    final_path: Path,
    narration_path: Path,
    script: Mapping[str, Any],
) -> dict[str, Any]:
    """Burn the restored three-event Short text layer onto the final picture."""
    timeline_path = Path(output_dir) / "short-voice-owned-timeline.json"
    if timeline_path.is_file():
        try:
            timeline_report = json.loads(timeline_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ShortTimedTextError("short_timed_text_voice_timeline_invalid") from exc
        if not isinstance(timeline_report, Mapping):
            raise ShortTimedTextError("short_timed_text_voice_timeline_invalid")
        events = build_events_from_voice_timeline(
            script=script,
            timeline_report=timeline_report,
        )
    else:
        # Compatibility for older artifacts/tests that predate the explicit
        # Voice-Owned Timeline certificate.
        events = build_events_from_section_audio(
            script=script,
            audio_dir=Path(output_dir) / "audio",
            mastered_narration=Path(narration_path),
        )
    rendered = Path(output_dir) / ".final-short-timed-text.mp4"
    report = render_progressive_text(
        video=Path(final_path),
        events=events,
        srt_path=Path(output_dir) / "short-timed-text.srt",
        output=rendered,
    )
    os.replace(rendered, Path(final_path))
    return {
        **report,
        "events": events,
        "final_file": Path(final_path).name,
    }

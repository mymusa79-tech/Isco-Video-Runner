from __future__ import annotations

import math
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .media import probe_duration

SCHEMA_VERSION = 1
RICH_RENDERER_VERSION = "clean-v2-short-rich-timed-text-v1"
ALLOWED_ROLES = {"hook", "beat", "payoff"}
ACCENT_ASS = "&H005BA8D7"  # RGB #D7A85B warm gold
PRIMARY_ASS = "&H00EAF1F4"  # RGB #F4F1EA warm off-white
OUTLINE_ASS = "&HA0000000"
MAX_DARK_SLATES = 1
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

    roles = ("hook", "beat", "payoff")
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

    # Preserve the exact section boundaries produced by the sectioned TTS files,
    # scaled only for tiny deterministic mastering-duration drift.
    scale = mastered_total / raw_total
    cursor = 0.0
    events: list[dict[str, object]] = []
    for index, (section, role, duration) in enumerate(zip(sections, roles, durations), start=1):
        if not isinstance(section, Mapping):
            raise ShortTimedTextError("short_timed_text_section_invalid")
        start = cursor
        end = mastered_total if index == 3 else cursor + duration * scale
        events.append(
            {
                "start": round(start, 3),
                "end": round(end, 3),
                "text": _select_event_text(section.get("narration"), role),
                "role": role,
            }
        )
        cursor = end
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
    if len(words) <= 3:
        return "", text
    focus_size = 2 if len(words) <= 8 else 3
    if role == "hook":
        return " ".join(words[focus_size:]), " ".join(words[:focus_size])
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
    normalized = tuple(validated or validate_progressive_text(events))
    if len(normalized) < 3:
        return None
    best_index: int | None = None
    best_score = 0
    for index, event in enumerate(normalized):
        score = _slate_score(event)
        if score > best_score:
            best_score = score
            best_index = index
    return best_index


def build_rich_ass(
    events: Sequence[Mapping[str, object]],
    *,
    slate_index: int | None = None,
) -> str:
    validated = validate_progressive_text(events)
    if slate_index is None:
        slate_index = choose_dark_slate_index(events, validated)

    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "PlayResX: 1080",
        "PlayResY: 1920",
        "YCbCr Matrix: TV.709",
        "",
        "[V4+ Styles]",
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding",
        f"Style: Body,Noto Sans Arabic,58,{PRIMARY_ASS},{PRIMARY_ASS},{OUTLINE_ASS},&H00000000,-1,0,0,0,100,100,0,0,1,3,0,5,80,80,0,1",
        f"Style: Focus,Noto Sans Arabic,70,{ACCENT_ASS},{ACCENT_ASS},{OUTLINE_ASS},&H00000000,-1,0,0,0,100,100,0,0,1,3,0,5,80,80,0,1",
        f"Style: SlateBody,Noto Sans Arabic,62,{PRIMARY_ASS},{PRIMARY_ASS},&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,0,0,5,80,80,0,1",
        f"Style: SlateFocus,Noto Sans Arabic,78,{ACCENT_ASS},{ACCENT_ASS},&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,0,0,5,80,80,0,1",
        "",
        "[Events]",
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
    ]

    for index, item in enumerate(validated):
        start = _ass_time(item.start)
        end = _ass_time(item.end)
        body, focus = split_focus_phrase(item.text, item.role)
        is_slate = index == slate_index
        if is_slate:
            body_y, focus_y = 875, 1030
            body_style, focus_style = "SlateBody", "SlateFocus"
            body_tag = r"\fad(120,170)"
            focus_tag = r"\fad(150,180)\t(0,200,\fscx103\fscy103)"
        else:
            focus_y = 1325 if item.role == "hook" else 1410
            if item.role == "payoff":
                focus_y = 1375
            body_y = focus_y + 105
            body_style, focus_style = "Body", "Focus"
            body_tag = r"\fad(80,140)"
            focus_tag = r"\fad(90,150)\t(0,180,\fscx103\fscy103)"
        if focus:
            lines.append(
                f"Dialogue: 1,{start},{end},{focus_style},,0,0,0,,"
                f"{{{{\\an5\\pos(540,{focus_y}){focus_tag}}}}}{_ass_escape(focus)}"
            )
        if body:
            lines.append(
                f"Dialogue: 0,{start},{end},{body_style},,0,0,0,,"
                f"{{{{\\an5\\pos(540,{body_y}){body_tag}}}}}{_ass_escape(body)}"
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
    slate_index = choose_dark_slate_index(events, validated)
    ass_path = Path(srt_path).with_suffix(".rich.ass")
    ass_path.write_text(build_rich_ass(events, slate_index=slate_index), encoding="utf-8")

    filters: list[str] = []
    if slate_index is not None:
        slate = validated[slate_index]
        filters.append(
            "drawbox=x=0:y=0:w=iw:h=ih:color=black@0.94:t=fill:"
            f"enable='between(t,{slate.start:.3f},{slate.end:.3f})'"
        )
    filters.append(f"subtitles='{_filter_escape_path(ass_path)}'")
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
        "renderer": "ffmpeg_libass_contextual_two_tone",
        "renderer_version": RICH_RENDERER_VERSION,
        "status": "pass",
        "srt": str(srt),
        "output": str(output),
        "portrait": True,
        "event_count": len(validated),
        "dark_slate_count": 1 if slate_index is not None else 0,
        "dark_slate_index": slate_index,
        "dark_slate_hook_forbidden": True,
        "max_dark_slates": MAX_DARK_SLATES,
        "accent_rgb": "#D7A85B",
        "body_rgb": "#F4F1EA",
        "provider_calls": 0,
        "word_level_alignment_claimed": False,
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

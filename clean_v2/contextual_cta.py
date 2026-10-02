from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping


# Literal Clean V2 port of the tested legacy cinematic_cta.py behavior.
CTA_CONTRACT_VERSION = "contextual-cta-v2"
MIN_CTA_START_SECONDS = 30.5
FINAL_QUIET_SECONDS = 12.0
DEFAULT_VISUAL_SECONDS = 3.6
MAX_SPOKEN_WORDS = 32
MAX_SCREEN_WORDS = 12
CTA_ACCENT_ASS = "&H005BA8D7"  # RGB #D7A85B, shared channel focus accent

_SECRET_ENV_NAMES = {
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "OPENROUTER_API_KEY",
    "MISTRAL_API_KEY",
    "PEXELS_API_KEY",
    "PIXABAY_API_KEY",
    "CLOUDFLARE_API_TOKEN",
    "YOUTUBE_API_KEY",
    "YOUTUBE_CLIENT_ID",
    "YOUTUBE_CLIENT_SECRET",
    "YOUTUBE_REFRESH_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "TELEGRAM_ALLOWED_USER_ID",
    "STATE_ENCRYPTION_KEY",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GEMINI_API_KEY_FILE",
    "GROQ_API_KEY_FILE",
    "OPENROUTER_API_KEY_FILE",
    "MISTRAL_API_KEY_FILE",
    "PEXELS_API_KEY_FILE",
    "PIXABAY_API_KEY_FILE",
    "CLOUDFLARE_API_TOKEN_FILE",
    "YOUTUBE_API_KEY_FILE",
    "YOUTUBE_CLIENT_ID_FILE",
    "YOUTUBE_CLIENT_SECRET_FILE",
    "YOUTUBE_REFRESH_TOKEN_FILE",
    "TELEGRAM_BOT_TOKEN_FILE",
    "TELEGRAM_CHAT_ID_FILE",
    "TELEGRAM_ALLOWED_USER_ID_FILE",
}


def _secret_free_subprocess_env() -> dict[str, str]:
    child_env = os.environ.copy()
    for name in _SECRET_ENV_NAMES:
        child_env.pop(name, None)
    return child_env


class CtaMode(str, Enum):
    COMMENT = "comment"
    SUBSCRIBE = "subscribe"
    SHARE = "share"
    LIKE = "like"
    NONE = "none"


@dataclass(frozen=True)
class CtaBinding:
    contract_version: str
    mode: CtaMode
    anchor_section_id: str | None
    spoken_text: str
    primary_text: str
    secondary_text: str
    visual_only: bool
    reason: str


@dataclass(frozen=True)
class CtaSchedule:
    start_seconds: float
    end_seconds: float
    anchor_section_id: str


_ACTION_MARKERS: dict[CtaMode, tuple[str, ...]] = {
    CtaMode.COMMENT: (
        "التعليقات",
        "تعليق",
        "اكتب",
        "أخبرني",
        "اخبرني",
        "شاركنا رأيك",
        "ما رأيك",
    ),
    CtaMode.SUBSCRIBE: (
        "اشترك",
        "الاشتراك",
        "انضم",
        "تابع القناة",
        "تكمل الرحلة",
        "واصل الرحلة",
    ),
    CtaMode.SHARE: (
        "شاركها",
        "شارك هذا",
        "شارك الفيديو",
        "أرسلها",
        "ارسلها",
        "أرسل هذا",
        "ارسل هذا",
    ),
    CtaMode.LIKE: (
        "إعجاب",
        "اعجاب",
        "أعجبك",
        "اعجبك",
        "أضافت لك",
        "اضافت لك",
    ),
}

_SENTENCE_END = re.compile(r"(?<=[.!؟!])\s+")


def _compact(text: object) -> str:
    return " ".join(str(text or "").replace("\n", " ").split()).strip()


def _first_sentence(text: str) -> str:
    text = _compact(text)
    if not text:
        return ""
    return _SENTENCE_END.split(text, maxsplit=1)[0].strip()


def _clip_words(text: str, maximum: int) -> str:
    words = _compact(text).split()
    if len(words) <= maximum:
        return " ".join(words)
    clipped = " ".join(words[:maximum]).rstrip("،؛:.- ")
    return clipped + "…"


def infer_cta_mode(cta_text: str) -> tuple[CtaMode, str]:
    text = _compact(cta_text)
    if not text:
        return CtaMode.NONE, "empty_cta"
    matched: list[CtaMode] = []
    for mode, markers in _ACTION_MARKERS.items():
        if any(marker in text for marker in markers):
            matched.append(mode)
    if not matched and "؟" in text:
        matched.append(CtaMode.COMMENT)
    unique = list(dict.fromkeys(matched))
    if len(unique) == 1:
        return unique[0], "single_action"
    if len(unique) > 1:
        return CtaMode.NONE, "bundled_actions_rejected"
    return CtaMode.NONE, "no_supported_action"


def _choose_anchor_section(plan: Any, mode: CtaMode) -> Any | None:
    """Choose one earned long-form CTA position without touching the hook/payoff.

    Film normally has five sections. Podcast may legitimately have only two, so
    a two-section episode uses the second section but the insertion helper keeps
    the CTA before that section's closing sentence.
    """
    sections = list(getattr(plan, "sections", []) or [])
    if len(sections) < 2:
        return None
    fmt = str(getattr(plan, "format", "") or "")
    ratios = {
        CtaMode.COMMENT: 0.56,
        CtaMode.LIKE: 0.54,
        CtaMode.SHARE: 0.68,
        CtaMode.SUBSCRIBE: 0.72,
    }
    ratio = ratios.get(mode, 0.58)
    if fmt == "podcast":
        ratio = max(ratio, 0.62)
    index = round((len(sections) - 1) * ratio)
    index = max(1, min(len(sections) - 1, index))
    if len(sections) >= 3:
        index = min(index, len(sections) - 2)
    return sections[index]


def _spoken_cta(authored: str, mode: CtaMode) -> str:
    """Keep the Planning-authored CTA intact only when it is concise and complete."""
    text = _compact(authored)
    if not text:
        return ""
    if len(text.split()) > MAX_SPOKEN_WORDS:
        return ""
    inferred, _ = infer_cta_mode(text)
    if inferred != mode:
        return ""
    if text[-1] not in ".!؟":
        text += "."
    return text


def _insert_spoken_cta(
    narration: str,
    spoken_text: str,
    *,
    fmt: str = "",
) -> tuple[str, int]:
    """Insert at a natural late-body boundary; Podcast CTA always belongs to B."""
    narration = _compact(narration)
    spoken_text = _compact(spoken_text)
    if not narration or not spoken_text:
        return narration, -1
    if spoken_text in narration:
        prefix = narration.split(spoken_text, 1)[0]
        return narration, len(prefix.split())

    if fmt == "podcast":
        turn_re = re.compile(r"(?<!\S)([AB]):\s+")
        matches = list(turn_re.finditer(narration))
        b_indexes = [
            index for index, match in enumerate(matches)
            if match.group(1) == "B"
        ]
        if not b_indexes:
            return narration, -1
        turn_index = b_indexes[-1]
        match = matches[turn_index]
        turn_start = match.end()
        turn_end = (
            matches[turn_index + 1].start()
            if turn_index + 1 < len(matches)
            else len(narration)
        )
        b_spoken = narration[turn_start:turn_end].strip()
        sentences = [
            item.strip() for item in _SENTENCE_END.split(b_spoken) if item.strip()
        ]
        if len(sentences) <= 1:
            insertion_at = turn_end
            updated = (
                narration[:turn_end].rstrip()
                + " "
                + spoken_text
                + " "
                + narration[turn_end:].lstrip()
            ).strip()
            return updated, len(narration[:insertion_at].split())

        before = " ".join(sentences[:-1]).strip()
        after = sentences[-1]
        replacement = f"{before} {spoken_text} {after}".strip()
        updated = (
            narration[:turn_start]
            + replacement
            + narration[turn_end:]
        ).strip()
        insertion_prefix = narration[:turn_start] + before
        return updated, len(insertion_prefix.split())

    sentences = [item.strip() for item in _SENTENCE_END.split(narration) if item.strip()]
    if len(sentences) <= 1:
        return f"{narration} {spoken_text}".strip(), len(narration.split())

    # Preserve the final sentence as the section's narrative landing. This makes
    # the CTA feel like a brief aside after value, then immediately returns to
    # the episode instead of ending the section on promotion.
    before = " ".join(sentences[:-1]).strip()
    after = sentences[-1]
    insertion_words = len(before.split())
    return f"{before} {spoken_text} {after}".strip(), insertion_words


def _screen_copy(mode: CtaMode, authored: str) -> tuple[str, str]:
    if mode == CtaMode.COMMENT:
        question = _clip_words(_first_sentence(authored), MAX_SCREEN_WORDS)
        return question or "ما الذي لم تنتبه له من قبل؟", "اكتبها في التعليقات"
    if mode == CtaMode.SUBSCRIBE:
        return "نداء اليقظة", "اشترك لتكمل الرحلة"
    if mode == CtaMode.SHARE:
        return "هل تعرف من يحتاج هذه الفكرة؟", "شاركها معه"
    if mode == CtaMode.LIKE:
        return "إذا أضافت لك الفكرة شيئًا", "إعجابك يكفي"
    return "", ""


def bind_contextual_cta(plan: Any) -> CtaBinding:
    if str(getattr(plan, "format", "")) in {"moment", "short"}:
        return CtaBinding(
            CTA_CONTRACT_VERSION,
            CtaMode.NONE,
            None,
            "",
            "",
            "",
            True,
            "short_no_cta" if str(getattr(plan, "format", "")) == "short" else "moment_no_cta",
        )

    authored = _compact(getattr(plan, "cta", ""))
    mode, reason = infer_cta_mode(authored)
    if mode == CtaMode.NONE:
        return CtaBinding(
            CTA_CONTRACT_VERSION, mode, None, "", "", "", True, reason
        )

    anchor = _choose_anchor_section(plan, mode)
    if anchor is None:
        return CtaBinding(
            CTA_CONTRACT_VERSION,
            CtaMode.NONE,
            None,
            "",
            "",
            "",
            True,
            "no_safe_anchor_section",
        )

    spoken = _spoken_cta(authored, mode)
    if not spoken:
        return CtaBinding(
            CTA_CONTRACT_VERSION,
            CtaMode.NONE,
            None,
            "",
            "",
            "",
            True,
            "spoken_cta_not_concise_or_invalid",
        )
    primary, secondary = _screen_copy(mode, authored)

    # Film/Podcast own exactly one contextual spoken CTA. Short remains excluded
    # above. The visual asset is scheduled against this same spoken request.
    return CtaBinding(
        CTA_CONTRACT_VERSION,
        mode,
        str(getattr(anchor, "id", "")) or None,
        spoken,
        primary,
        secondary,
        False,
        reason,
    )


def schedule_cta(
    binding: CtaBinding,
    section_ids: list[str],
    section_durations: list[float],
    *,
    spoken_fraction: float | None = None,
) -> CtaSchedule | None:
    if binding.mode == CtaMode.NONE or not binding.anchor_section_id:
        return None
    if len(section_ids) != len(section_durations) or not section_ids:
        return None
    try:
        index = section_ids.index(binding.anchor_section_id)
    except ValueError:
        return None

    starts: list[float] = []
    cursor = 0.0
    for seconds in section_durations:
        starts.append(cursor)
        cursor += max(0.0, float(seconds))
    total = cursor
    section_start = starts[index]
    section_end = section_start + max(0.0, float(section_durations[index]))
    latest_end = max(0.0, total - FINAL_QUIET_SECONDS)
    if section_end <= MIN_CTA_START_SECONDS or latest_end <= MIN_CTA_START_SECONDS:
        return None

    if spoken_fraction is None:
        anchor_fraction = 0.72
    else:
        anchor_fraction = max(0.40, min(0.90, float(spoken_fraction)))
    desired_start = section_start + max(
        0.8,
        (section_end - section_start) * anchor_fraction,
    )
    start = max(MIN_CTA_START_SECONDS, desired_start)
    end = min(start + DEFAULT_VISUAL_SECONDS, section_end - 0.35, latest_end)
    if end - start < 2.2:
        start = max(
            MIN_CTA_START_SECONDS,
            min(section_end, latest_end) - DEFAULT_VISUAL_SECONDS,
        )
        end = min(start + DEFAULT_VISUAL_SECONDS, section_end - 0.2, latest_end)
    if end - start < 2.2:
        return None
    return CtaSchedule(round(start, 3), round(end, 3), binding.anchor_section_id)


def _ass_time(seconds: float) -> str:
    centis = int(round(seconds * 100))
    hours, rem = divmod(centis, 360000)
    minutes, rem = divmod(rem, 6000)
    secs, cs = divmod(rem, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{cs:02d}"


def _ass_escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")


def build_cta_ass(binding: CtaBinding, schedule: CtaSchedule) -> str:
    start = _ass_time(schedule.start_seconds)
    end = _ass_time(schedule.end_seconds)
    primary = _ass_escape(binding.primary_text)
    secondary = _ass_escape(binding.secondary_text)
    return "\n".join(
        [
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
            "Style: CTA,Cairo,52,&H00F4F1EA,&H00F4F1EA,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,4,0,5,0,0,0,1",
            f"Style: CTASub,Cairo,38,{CTA_ACCENT_ASS},{CTA_ACCENT_ASS},&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,4,0,5,0,0,0,1",
            "",
            "[Events]",
            "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
            f"Dialogue: 0,{start},{end},CTA,,0,0,0,,{{\\an5\\pos(1395,142)\\fad(180,220)}}{primary}",
            f"Dialogue: 1,{start},{end},CTASub,,0,0,0,,{{\\an5\\pos(1395,205)\\fad(180,220)}}{secondary}",
            "",
        ]
    )


def _filter_escape_path(path: Path) -> str:
    return (
        str(path.resolve())
        .replace("\\", "/")
        .replace(":", r"\:")
        .replace("'", r"\'")
    )


def render_cta_overlay(
    video: Path,
    binding: CtaBinding,
    schedule: CtaSchedule,
    dest: Path,
) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    ass_path = dest.with_suffix(".cta.ass")
    ass_path.write_text(build_cta_ass(binding, schedule), encoding="utf-8")
    vf = f"subtitles='{_filter_escape_path(ass_path)}'"
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(video),
                "-vf",
                vf,
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "18",
                "-c:a",
                "copy",
                str(dest),
            ],
            check=True,
            env=_secret_free_subprocess_env(),
        )
    finally:
        ass_path.unlink(missing_ok=True)
    return dest


def write_cta_report(
    path: Path,
    binding: CtaBinding,
    schedule: CtaSchedule | None,
) -> Path:
    payload = {
        **asdict(binding),
        "mode": binding.mode.value,
        "schedule": asdict(schedule) if schedule is not None else None,
        "rules": {
            "opening_cta_forbidden_before_seconds": MIN_CTA_START_SECONDS,
            "final_quiet_seconds": FINAL_QUIET_SECONDS,
            "one_primary_action": True,
            "short_spoken_social_cta_forbidden": True,
            "longform_spoken_cta_count": 1 if binding.spoken_text else 0,
            "provider_calls": 0,
            "action_accent_rgb": "#D7A85B",
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def bind_contextual_cta_to_script(
    *,
    output_dir: Path,
    brief: Mapping[str, Any],
    plan: Mapping[str, Any],
    script: dict[str, Any],
) -> dict[str, Any]:
    sections = [
        SimpleNamespace(
            id=str(item.get("id") or ""),
            narration=str(item.get("narration") or ""),
        )
        for item in (script.get("sections") or [])
        if isinstance(item, Mapping)
    ]
    runtime_plan = SimpleNamespace(
        format=str(brief.get("format") or ""),
        cta=str(plan.get("cta") or ""),
        sections=sections,
    )
    binding = bind_contextual_cta(runtime_plan)

    insertion_word_offset = None
    if (
        binding.mode != CtaMode.NONE
        and binding.anchor_section_id
        and binding.spoken_text
        and not binding.visual_only
    ):
        for item in script.get("sections") or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("id") or "") != binding.anchor_section_id:
                continue
            updated, offset = _insert_spoken_cta(
                str(item.get("narration") or ""),
                binding.spoken_text,
                fmt=str(brief.get("format") or ""),
            )
            item["narration"] = updated
            insertion_word_offset = offset
            break

    report_path = Path(output_dir) / "cta-plan.json"
    write_cta_report(report_path, binding, None)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["source"] = "legacy-cinematic-cta-port"
    report["binding_phase"] = "pre_tts"
    report["provider_calls_added"] = 0
    report["spoken_insertion_word_offset"] = insertion_word_offset
    report["spoken_alignment_policy"] = "one_contextual_longform_cta"
    report["render_status"] = "pending"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def _timeline_section_durations(
    output_dir: Path,
    section_ids: list[str],
    total_seconds: float,
) -> tuple[list[float], str]:
    """Prefer measured Timeline First section spans; fall back deterministically."""
    path = Path(output_dir) / "timeline-first.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}
    durations: dict[str, float] = {}
    for event in raw.get("section_events") or []:
        if not isinstance(event, Mapping):
            continue
        section_id = str(event.get("section_id") or "").strip()
        if section_id not in section_ids:
            continue
        try:
            start = float(event.get("start"))
            end = float(event.get("end"))
        except (TypeError, ValueError):
            continue
        span = end - start
        if span > 0:
            durations[section_id] = durations.get(section_id, 0.0) + span

    if all(durations.get(section_id, 0.0) > 0 for section_id in section_ids):
        return [durations[section_id] for section_id in section_ids], "timeline-first"
    equal = max(0.0, float(total_seconds)) / max(1, len(section_ids))
    return [equal for _ in section_ids], "equal-fallback"


def apply_contextual_cta_overlay(
    *,
    output_dir: Path,
    final_path: Path,
    narration_path: Path,
    script: Mapping[str, Any],
) -> dict[str, Any]:
    from clean_v2.media import probe_duration

    output_dir = Path(output_dir)
    report_path = output_dir / "cta-plan.json"
    raw = json.loads(report_path.read_text(encoding="utf-8"))
    binding = CtaBinding(
        contract_version=str(raw.get("contract_version") or ""),
        mode=CtaMode(str(raw.get("mode") or "none")),
        anchor_section_id=(
            str(raw.get("anchor_section_id"))
            if raw.get("anchor_section_id") is not None
            else None
        ),
        spoken_text=str(raw.get("spoken_text") or ""),
        primary_text=str(raw.get("primary_text") or ""),
        secondary_text=str(raw.get("secondary_text") or ""),
        visual_only=bool(raw.get("visual_only")),
        reason=str(raw.get("reason") or ""),
    )

    section_ids = [
        str(item.get("id") or "")
        for item in (script.get("sections") or [])
        if isinstance(item, Mapping) and str(item.get("id") or "")
    ]
    if not section_ids:
        raise RuntimeError("contextual CTA requires script section ids")
    total = probe_duration(Path(narration_path))
    section_durations, section_duration_source = _timeline_section_durations(
        output_dir,
        section_ids,
        total,
    )
    spoken_fraction = None
    raw_offset = raw.get("spoken_insertion_word_offset")
    if (
        binding.anchor_section_id
        and isinstance(raw_offset, int)
        and raw_offset >= 0
        and binding.spoken_text
    ):
        anchor_narration = next(
            (
                str(item.get("narration") or "")
                for item in (script.get("sections") or [])
                if isinstance(item, Mapping)
                and str(item.get("id") or "") == binding.anchor_section_id
            ),
            "",
        )
        total_words = max(1, len(_compact(anchor_narration).split()))
        spoken_fraction = min(0.95, raw_offset / total_words)

    schedule = schedule_cta(
        binding,
        section_ids,
        section_durations,
        spoken_fraction=spoken_fraction,
    )
    write_cta_report(report_path, binding, schedule)

    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["spoken_insertion_word_offset"] = raw.get("spoken_insertion_word_offset")
    report["spoken_alignment_policy"] = raw.get(
        "spoken_alignment_policy",
        "one_contextual_longform_cta",
    )
    report["spoken_fraction_estimate"] = spoken_fraction
    report["section_duration_source"] = section_duration_source
    report["source"] = "legacy-cinematic-cta-port"
    report["binding_phase"] = "pre_tts"
    report["provider_calls_added"] = 0

    if schedule is None:
        report["render_status"] = "not_scheduled"
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return report

    # The legacy ASS/text card is intentionally no longer rendered. The authored
    # action and safe schedule remain useful planning evidence, while the visual layer
    # is owned by visual_cta.py and uses only the user's approved original assets.
    report["render_status"] = "delegated_to_approved_visual_assets"
    report["visual_owner"] = "clean_v2.visual_cta"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report

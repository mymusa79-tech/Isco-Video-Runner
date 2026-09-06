from __future__ import annotations

"""Contextual visual punctuation for long-form production.

This is a zero-provider-call finishing layer. It turns already-authored plan
semantics into a small number of restrained editorial interventions:
- text over the current picture,
- one optional dark full-frame slate,
- warm-accent focus phrases.

It runs after M10 and CTA so it can respect their actual schedules instead of
guessing. Render failure is non-blocking polish: production keeps the already
finished M10/CTA picture.
"""

import json
import re
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import isco_video_agent.orchestrator as orchestrator
from isco_video_agent.security import secret_free_subprocess_env


REPORT_NAME = "visual-punctuation.json"
PLAN_NAME = "plan.json"
TIMELINE_NAME = "visual-timeline.json"
CTA_REPORT_NAME = "cta-plan.json"
M10_REPORT_NAME = "m10-cards.json"

CONTRACT_VERSION = "contextual-visual-punctuation-v1"
OPENING_GUARD_SECONDS = 28.0
ENDING_GUARD_SECONDS = 16.0
MAX_EVENTS = 3
MAX_SLATES = 1
MIN_COOLDOWN_SECONDS = 34.0
OVERLAY_SECONDS = 3.3
SLATE_SECONDS = 2.8
MAX_AUTHORED_WORDS = 14

# ASS BGR notation. The accent is intentionally warm and restrained to fit the
# channel's reflective visual language instead of looking like platform UI.
ACCENT_ASS = "&H005BA8D7"       # RGB #D7A85B
PRIMARY_ASS = "&H00F4F1EA"      # warm off-white
MUTED_ASS = "&H00D2CEC5"
OUTLINE_ASS = "&H90000000"

_QUOTE_PATTERNS = (
    re.compile(r"«[^»]{18,180}»"),
    re.compile(r"“[^”]{18,180}”"),
    re.compile(r'"[^"\n]{18,180}"'),
)
_STAT_PATTERN = re.compile(r"(?<!\w)\d{1,3}(?:[.,]\d{1,2})?\s*(?:%|٪|بالمئة|في المئة)(?!\w)")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!؟!])\s+")

_REFRAME_MARKERS = (
    "لكن", "بل ", "الحقيقة", "المشكلة", "المفارقة", "في الواقع",
    "ما لا ننتبه", "ما لا تراه", "الأهم", "الفكرة ليست", "ليس ",
)
_TURN_MARKERS = (
    "لذلك", "لهذا", "من هنا", "الآن", "ابدأ", "تذكر", "جرّب", "جرب",
    "الخطوة", "القرار", "يكفي", "يمكنك", "تستطيع",
)
_EMOTIONAL_MARKERS = (
    "الخوف", "القلق", "الاستنزاف", "المقارنة", "السقوط", "الفشل",
    "الهدوء", "النجاة", "الأمل", "النهوض", "العودة", "التحرر",
)


def _clean(value: object) -> str:
    return " ".join(str(value or "").replace("\n", " ").split()).strip()


def _word_count(text: str) -> int:
    return len(_clean(text).split())


def _first_sentence(text: str) -> str:
    compact = _clean(text)
    if not compact:
        return ""
    return _SENTENCE_SPLIT.split(compact, maxsplit=1)[0].strip()


def _clip_words(text: str, maximum: int) -> str:
    words = _clean(text).split()
    if len(words) <= maximum:
        return " ".join(words)
    return " ".join(words[:maximum]).rstrip("،؛:.- ") + "…"


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _has_m10_evidence(narration: str) -> bool:
    return any(pattern.search(narration) for pattern in _QUOTE_PATTERNS) or bool(_STAT_PATTERN.search(narration))


def _section_spans(timeline: dict[str, Any]) -> tuple[dict[str, tuple[float, float]], float]:
    shots = timeline.get("final_cut_visuals")
    if not isinstance(shots, list) or not shots:
        return {}, 0.0
    spans: dict[str, tuple[float, float]] = {}
    total = 0.0
    for shot in shots:
        if not isinstance(shot, dict):
            continue
        section_id = _clean(shot.get("section_id"))
        if not section_id:
            continue
        try:
            start = float(shot.get("start_seconds"))
            end = float(shot.get("end_seconds"))
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        if section_id in spans:
            old_start, old_end = spans[section_id]
            spans[section_id] = (min(old_start, start), max(old_end, end))
        else:
            spans[section_id] = (start, end)
        total = max(total, end)
    return spans, total


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _reserved_intervals(root: Path) -> list[tuple[float, float, str]]:
    intervals: list[tuple[float, float, str]] = []

    cta = _read_json(root / CTA_REPORT_NAME)
    schedule = cta.get("schedule")
    if isinstance(schedule, dict):
        try:
            start = float(schedule.get("start_seconds"))
            end = float(schedule.get("end_seconds"))
        except (TypeError, ValueError):
            pass
        else:
            if end > start:
                intervals.append((start - 0.45, end + 0.45, "cta"))

    m10 = _read_json(root / M10_REPORT_NAME)
    cards = m10.get("cards")
    if isinstance(cards, list):
        for card in cards:
            if not isinstance(card, dict):
                continue
            try:
                start = float(card.get("start_seconds"))
                end = float(card.get("end_seconds"))
            except (TypeError, ValueError):
                continue
            if end > start:
                intervals.append((start - 0.35, end + 0.35, "m10"))
    return intervals


def _overlaps(start: float, end: float, intervals: list[tuple[float, float, str]]) -> bool:
    return any(start < reserved_end and end > reserved_start for reserved_start, reserved_end, _ in intervals)


def _authored_text(section: dict[str, Any]) -> tuple[str, str]:
    """Prefer plan-authored screen copy; never invent a factual statement."""
    on_screen = _clean(section.get("on_screen_text"))
    if 2 <= _word_count(on_screen) <= MAX_AUTHORED_WORDS:
        return on_screen, "on_screen_text"
    key_point = _clean(section.get("key_point"))
    if 2 <= _word_count(key_point) <= MAX_AUTHORED_WORDS:
        return key_point, "key_point"
    narration = _first_sentence(str(section.get("narration") or ""))
    if 3 <= _word_count(narration) <= 11:
        return narration, "verbatim_narration_sentence"
    return "", ""


def _split_focus(text: str) -> tuple[str, str]:
    """Return (body, focus) without rewriting the authored words."""
    text = _clean(text)
    if not text:
        return "", ""
    for separator in (" — ", ": ", "؛ ", "، بل "):
        if separator in text:
            left, right = text.rsplit(separator, 1)
            if 1 <= _word_count(right) <= 5 and _word_count(left) >= 1:
                return left.rstrip("،؛:- "), right.strip()
    words = text.split()
    if len(words) <= 3:
        return "", text
    focus_size = 2 if len(words) <= 8 else 3
    return " ".join(words[:-focus_size]), " ".join(words[-focus_size:])


def _candidate_score(section: dict[str, Any], text_source: str) -> tuple[int, list[str]]:
    narration = _clean(section.get("narration"))
    authored = _clean(section.get("on_screen_text"))
    score = 0
    reasons: list[str] = []

    if text_source == "on_screen_text":
        score += 4
        reasons.append("authored_on_screen_text")
    elif text_source == "key_point":
        score += 3
        reasons.append("authored_key_point")
    elif text_source == "verbatim_narration_sentence":
        score += 1
        reasons.append("short_verbatim_sentence")

    if "؟" in narration:
        score += 2
        reasons.append("question_turn")
    if _contains_any(narration, _REFRAME_MARKERS):
        score += 2
        reasons.append("reframe")
    if _contains_any(narration, _TURN_MARKERS):
        score += 2
        reasons.append("action_or_turn")
    if _contains_any(narration, _EMOTIONAL_MARKERS):
        score += 1
        reasons.append("emotional_anchor")
    if authored and _word_count(authored) <= 8:
        score += 1
        reasons.append("compact_authored_copy")
    return score, reasons


def plan_visual_punctuation(
    plan: dict[str, Any],
    timeline: dict[str, Any],
    *,
    reserved: list[tuple[float, float, str]] | None = None,
) -> dict[str, Any]:
    if _clean(plan.get("format")).lower() == "moment":
        return {
            "version": CONTRACT_VERSION,
            "status": "not_applicable_moment",
            "events": [],
            "zero_additional_ai_calls": True,
        }

    sections = plan.get("sections")
    if not isinstance(sections, list):
        return {
            "version": CONTRACT_VERSION,
            "status": "skipped_invalid_plan",
            "events": [],
            "zero_additional_ai_calls": True,
        }

    spans, total = _section_spans(timeline)
    if not spans or total <= 0:
        return {
            "version": CONTRACT_VERSION,
            "status": "skipped_invalid_timeline",
            "events": [],
            "zero_additional_ai_calls": True,
        }

    reserved = list(reserved or [])
    candidates: list[dict[str, Any]] = []
    for order, section in enumerate(sections):
        if not isinstance(section, dict):
            continue
        section_id = _clean(section.get("id"))
        span = spans.get(section_id)
        if not section_id or span is None:
            continue
        section_start, section_end = span
        if section_start < OPENING_GUARD_SECONDS or section_end > total - ENDING_GUARD_SECONDS:
            continue

        narration = _clean(section.get("narration"))
        if _has_m10_evidence(narration):
            # M10 already owns explicit quotes/stats. Avoid double-punctuation.
            continue

        text, source = _authored_text(section)
        if not text:
            continue
        score, reasons = _candidate_score(section, source)
        if score < 4:
            continue

        span_seconds = section_end - section_start
        desired_start = section_start + min(2.2, max(0.8, span_seconds * 0.18))
        strong_slate = (
            score >= 7
            and _word_count(text) <= 8
            and (_contains_any(narration, _REFRAME_MARKERS) or _contains_any(narration, _TURN_MARKERS))
        )
        kind = "dark_slate" if strong_slate else "picture_emphasis"
        duration = SLATE_SECONDS if kind == "dark_slate" else OVERLAY_SECONDS
        latest_end = min(section_end - 0.35, total - ENDING_GUARD_SECONDS)
        end = min(desired_start + duration, latest_end)
        start = max(section_start + 0.35, end - duration)
        if end - start < 1.8 or _overlaps(start, end, reserved):
            continue

        body, focus = _split_focus(text)
        candidates.append(
            {
                "section_id": section_id,
                "section_order": order,
                "kind": kind,
                "start_seconds": round(start, 3),
                "end_seconds": round(end, 3),
                "body_text": body,
                "focus_text": focus,
                "source_text": text,
                "source": source,
                "score": score,
                "reasons": reasons,
                "source_authored_or_verbatim": True,
            }
        )

    # Pick strongest moments first, then restore chronological order. This gives the
    # layer editorial selectivity without random placement or another model call.
    ranked = sorted(
        candidates,
        key=lambda item: (-int(item["score"]), int(item["section_order"])),
    )
    selected: list[dict[str, Any]] = []
    slate_count = 0
    for candidate in ranked:
        if len(selected) >= MAX_EVENTS:
            break
        if candidate["kind"] == "dark_slate" and slate_count >= MAX_SLATES:
            candidate = {**candidate, "kind": "picture_emphasis"}
        start = float(candidate["start_seconds"])
        end = float(candidate["end_seconds"])
        if any(abs(start - float(existing["start_seconds"])) < MIN_COOLDOWN_SECONDS for existing in selected):
            continue
        if _overlaps(start, end, reserved):
            continue
        selected.append(candidate)
        if candidate["kind"] == "dark_slate":
            slate_count += 1

    selected.sort(key=lambda item: float(item["start_seconds"]))
    return {
        "version": CONTRACT_VERSION,
        "status": "applied" if selected else "no_eligible_events",
        "events": selected,
        "rules": {
            "max_events": MAX_EVENTS,
            "max_dark_slates": MAX_SLATES,
            "opening_guard_seconds": OPENING_GUARD_SECONDS,
            "ending_guard_seconds": ENDING_GUARD_SECONDS,
            "min_cooldown_seconds": MIN_COOLDOWN_SECONDS,
            "m10_and_cta_collision_avoidance": True,
            "provider_calls": 0,
            "random_placement": False,
            "invented_claims_allowed": False,
            "accent_rgb": "#D7A85B",
        },
        "zero_additional_ai_calls": True,
        "production_blocking": False,
    }


def _ass_time(seconds: float) -> str:
    centis = int(round(max(0.0, seconds) * 100))
    hours, rem = divmod(centis, 360000)
    minutes, rem = divmod(rem, 6000)
    secs, cs = divmod(rem, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{cs:02d}"


def _ass_escape(text: str) -> str:
    return (
        _clean(text)
        .replace("\\", r"\\")
        .replace("{", r"\{")
        .replace("}", r"\}")
    )


def build_visual_punctuation_ass(events: list[dict[str, Any]]) -> str:
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
        f"Style: VPBody,Noto Sans Arabic,48,{PRIMARY_ASS},{PRIMARY_ASS},{OUTLINE_ASS},&H00000000,0,0,0,0,100,100,0,0,1,2,0,5,0,0,0,1",
        f"Style: VPFocus,Noto Sans Arabic,58,{ACCENT_ASS},{ACCENT_ASS},{OUTLINE_ASS},&H00000000,-1,0,0,0,100,100,0,0,1,2,0,5,0,0,0,1",
        f"Style: SlateBody,Noto Sans Arabic,54,{PRIMARY_ASS},{PRIMARY_ASS},&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1",
        f"Style: SlateFocus,Noto Sans Arabic,68,{ACCENT_ASS},{ACCENT_ASS},&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1",
        "",
        "[Events]",
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
    ]
    for event in events:
        start = _ass_time(float(event["start_seconds"]))
        end = _ass_time(float(event["end_seconds"]))
        body = _ass_escape(str(event.get("body_text") or ""))
        focus = _ass_escape(str(event.get("focus_text") or ""))
        if event.get("kind") == "dark_slate":
            body_y, focus_y = 470, 565
            body_style, focus_style = "SlateBody", "SlateFocus"
            body_size = r"\fad(180,220)"
            focus_size = r"\fad(230,220)\t(0,220,\fscx103\fscy103)"
        else:
            body_y, focus_y = 650, 722
            body_style, focus_style = "VPBody", "VPFocus"
            body_size = r"\fad(160,220)"
            focus_size = r"\fad(200,220)\t(0,200,\fscx103\fscy103)"
        if body:
            lines.append(
                f"Dialogue: 0,{start},{end},{body_style},,0,0,0,,"
                f"{{\\an5\\pos(960,{body_y}){body_size}}}{body}"
            )
        if focus:
            lines.append(
                f"Dialogue: 1,{start},{end},{focus_style},,0,0,0,,"
                f"{{\\an5\\pos(960,{focus_y}){focus_size}}}{focus}"
            )
    lines.append("")
    return "\n".join(lines)


def _filter_escape_path(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")


def render_visual_punctuation(video: Path, events: list[dict[str, Any]], dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    ass = dest.with_suffix(".visual-punctuation.ass")
    ass.write_text(build_visual_punctuation_ass(events), encoding="utf-8")
    filters: list[str] = []
    for event in events:
        if event.get("kind") != "dark_slate":
            continue
        start = float(event["start_seconds"])
        end = float(event["end_seconds"])
        filters.append(
            "drawbox=x=0:y=0:w=iw:h=ih:color=black@0.96:t=fill:"
            f"enable='between(t,{start:.3f},{end:.3f})'"
        )
    # Picture-emphasis gets only a soft lower-middle readability veil; it is not a
    # permanent template box and is enabled only for the authored event interval.
    for event in events:
        if event.get("kind") != "picture_emphasis":
            continue
        start = float(event["start_seconds"])
        end = float(event["end_seconds"])
        filters.append(
            "drawbox=x=260:y=585:w=1400:h=205:color=black@0.28:t=fill:"
            f"enable='between(t,{start:.3f},{end:.3f})'"
        )
    filters.append(f"subtitles='{_filter_escape_path(ass)}'")
    vf = ",".join(filters)
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(video),
                "-vf", vf,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                "-c:a", "copy", str(dest),
            ],
            check=True,
            env=secret_free_subprocess_env(),
        )
    finally:
        ass.unlink(missing_ok=True)
    return dest


def _write_report(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


@contextmanager
def visual_punctuation_live_scope() -> Iterator[None]:
    original_mux = orchestrator.mux

    def mux_bound(video, narration, output, music=None, **kwargs):
        output = Path(output)
        if output.name != "final.mp4":
            return original_mux(video, narration, output, music, **kwargs)

        root = output.parent
        report_path = root / REPORT_NAME
        try:
            plan = _read_json(root / PLAN_NAME)
            timeline = _read_json(root / TIMELINE_NAME)
            if not plan or not timeline:
                raise ValueError("planning_artifact_unavailable")
        except Exception as exc:
            _write_report(
                report_path,
                {
                    "version": CONTRACT_VERSION,
                    "status": "skipped",
                    "reason": f"planning_artifact_unavailable:{type(exc).__name__}",
                    "events": [],
                    "zero_additional_ai_calls": True,
                    "production_blocking": False,
                },
            )
            return original_mux(video, narration, output, music, **kwargs)

        reserved = _reserved_intervals(root)
        policy = plan_visual_punctuation(plan, timeline, reserved=reserved)
        policy["reserved_intervals"] = [
            {"start_seconds": round(start, 3), "end_seconds": round(end, 3), "owner": owner}
            for start, end, owner in reserved
        ]
        policy["production_stage"] = "pre_final_mux_after_m10_cta"
        _write_report(report_path, policy)
        events = policy.get("events") or []
        if not events:
            return original_mux(video, narration, output, music, **kwargs)

        rendered = root / ".visual-punctuation.mp4"
        try:
            render_visual_punctuation(Path(video), list(events), rendered)
            return original_mux(rendered, narration, output, music, **kwargs)
        except Exception as exc:
            fallback = dict(policy)
            fallback["status"] = "render_error_fallback_to_prior_cinematic_picture"
            fallback["render_error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
            _write_report(report_path, fallback)
            return original_mux(video, narration, output, music, **kwargs)
        finally:
            rendered.unlink(missing_ok=True)
            rendered.with_suffix(".visual-punctuation.ass").unlink(missing_ok=True)

    orchestrator.mux = mux_bound
    try:
        yield
    finally:
        orchestrator.mux = original_mux


def install_visual_punctuation_live_binding() -> None:
    current = orchestrator.produce
    if getattr(current, "_isco_visual_punctuation_live_binding", False):
        return

    def wrapped(*args, **kwargs):
        with visual_punctuation_live_scope():
            return current(*args, **kwargs)

    wrapped._isco_visual_punctuation_live_binding = True
    wrapped._isco_visual_punctuation_original = current
    orchestrator.produce = wrapped

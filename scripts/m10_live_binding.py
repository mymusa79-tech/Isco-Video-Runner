from __future__ import annotations

import json
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import isco_video_agent.orchestrator as orchestrator
from isco_video_agent.cinematic_m10_cards import CardLayout, CardRequest, CardType, Rect, render_card


_REPORT_NAME = "m10-cards.json"
_TIMELINE_NAME = "visual-timeline.json"
_PLAN_NAME = "plan.json"
_OPENING_GUARD_SECONDS = 30.0
_ENDING_GUARD_SECONDS = 12.0
_MAX_CARDS = 2

_QUOTE_PATTERNS = (
    re.compile(r"«([^»]{18,180})»"),
    re.compile(r"“([^”]{18,180})”"),
    re.compile(r'"([^"\n]{18,180})"'),
)
_STAT_PATTERN = re.compile(r"(?<!\w)(\d{1,3}(?:[.,]\d{1,2})?\s*(?:%|٪|بالمئة|في المئة))(?!\w)")


def _clean(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def _quote_from(text: str) -> str:
    for pattern in _QUOTE_PATTERNS:
        match = pattern.search(text)
        if match:
            return _clean(match.group(1))
    return ""


def _stat_from(text: str) -> str:
    match = _STAT_PATTERN.search(text)
    return _clean(match.group(1)) if match else ""


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


def plan_evidence_cards(plan: dict[str, Any], timeline: dict[str, Any]) -> dict[str, Any]:
    if _clean(plan.get("format")).lower() == "moment":
        return {
            "version": "m10-evidence-cards-v1",
            "status": "not_applicable_moment",
            "cards": [],
            "zero_additional_ai_calls": True,
            "invented_claims_allowed": False,
        }
    sections = plan.get("sections")
    if not isinstance(sections, list):
        return {
            "version": "m10-evidence-cards-v1",
            "status": "skipped_invalid_plan",
            "cards": [],
            "zero_additional_ai_calls": True,
            "invented_claims_allowed": False,
        }
    spans, total = _section_spans(timeline)
    if not spans or total <= 0:
        return {
            "version": "m10-evidence-cards-v1",
            "status": "skipped_invalid_timeline",
            "cards": [],
            "zero_additional_ai_calls": True,
            "invented_claims_allowed": False,
        }

    cards: list[dict[str, Any]] = []
    for section in sections:
        if len(cards) >= _MAX_CARDS or not isinstance(section, dict):
            break
        section_id = _clean(section.get("id"))
        span = spans.get(section_id)
        if not span:
            continue
        start, end = span
        if start < _OPENING_GUARD_SECONDS or end > total - _ENDING_GUARD_SECONDS:
            continue
        narration = str(section.get("narration") or "")
        on_screen = _clean(section.get("on_screen_text"))[:90]
        quote = _quote_from(narration)
        stat = _stat_from(narration)
        kind = ""
        primary = ""
        evidence = ""
        duration_seconds = 0.0
        if quote:
            kind = "quote"
            primary = quote
            evidence = "verbatim_explicit_quote_in_narration"
            duration_seconds = 4.5
        elif stat:
            kind = "stat"
            primary = stat
            evidence = "verbatim_explicit_numeric_stat_in_narration"
            duration_seconds = 3.8
        else:
            continue

        card_start = start + min(1.0, max(0.4, (end - start) * 0.12))
        latest = min(end - 0.35, total - _ENDING_GUARD_SECONDS)
        card_end = min(card_start + duration_seconds, latest)
        if card_end - card_start < 1.5:
            continue
        cards.append(
            {
                "section_id": section_id,
                "kind": kind,
                "start_seconds": round(card_start, 3),
                "end_seconds": round(card_end, 3),
                "primary_text": primary,
                "secondary_text": on_screen,
                "evidence": evidence,
                "source_text_verbatim": True,
            }
        )

    return {
        "version": "m10-evidence-cards-v1",
        "status": "applied" if cards else "no_eligible_cards",
        "cards": cards,
        "max_cards": _MAX_CARDS,
        "opening_guard_seconds": _OPENING_GUARD_SECONDS,
        "ending_guard_seconds": _ENDING_GUARD_SECONDS,
        "zero_additional_ai_calls": True,
        "invented_claims_allowed": False,
        "supported_types": ["quote", "stat"],
    }


def _layout() -> CardLayout:
    # Kept below the CTA top-right zone and above the subtitle-reserved lower band.
    return CardLayout(
        canvas_width=1920,
        canvas_height=1080,
        safe_zone=Rect(160, 270, 1600, 410),
        card_box=Rect(320, 310, 1280, 320),
        font_name="Noto Sans Arabic",
        primary_font_size=56,
        secondary_font_size=32,
    )


def _write_report(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


@contextmanager
def m10_live_scope() -> Iterator[None]:
    original_mux = orchestrator.mux

    def mux_bound(video, narration, output, music=None, **kwargs):
        output = Path(output)
        if output.name != "final.mp4":
            return original_mux(video, narration, output, music, **kwargs)
        root = output.parent
        report_path = root / _REPORT_NAME
        try:
            plan = json.loads((root / _PLAN_NAME).read_text(encoding="utf-8"))
            timeline = json.loads((root / _TIMELINE_NAME).read_text(encoding="utf-8"))
        except Exception as exc:
            _write_report(report_path, {
                "version": "m10-evidence-cards-v1",
                "status": "skipped",
                "reason": f"planning_artifact_unavailable:{type(exc).__name__}",
                "cards": [],
                "zero_additional_ai_calls": True,
                "invented_claims_allowed": False,
            })
            return original_mux(video, narration, output, music, **kwargs)

        policy = plan_evidence_cards(plan, timeline)
        _write_report(report_path, policy)
        cards = policy.get("cards") or []
        if not cards:
            return original_mux(video, narration, output, music, **kwargs)

        temp_dir = root / ".m10"
        temp_dir.mkdir(parents=True, exist_ok=True)
        current = Path(video)
        generated: list[Path] = []
        try:
            for index, item in enumerate(cards, 1):
                dest = temp_dir / f"card-{index:02d}.mp4"
                request = CardRequest(
                    card_type=CardType.QUOTE if item["kind"] == "quote" else CardType.STAT,
                    primary_text=str(item["primary_text"]),
                    secondary_text=str(item.get("secondary_text") or ""),
                    start_seconds=float(item["start_seconds"]),
                    end_seconds=float(item["end_seconds"]),
                )
                current = render_card(current, request, _layout(), dest)
                generated.append(Path(current))
            return original_mux(current, narration, output, music, **kwargs)
        except Exception as exc:
            fallback = dict(policy)
            fallback["status"] = "render_error_fallback_to_uncarded_video"
            fallback["render_error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
            _write_report(report_path, fallback)
            return original_mux(video, narration, output, music, **kwargs)
        finally:
            for item in generated:
                item.unlink(missing_ok=True)
                item.with_suffix(".m10.ass").unlink(missing_ok=True)
            for ass in temp_dir.glob("*.m10.ass"):
                ass.unlink(missing_ok=True)
            try:
                temp_dir.rmdir()
            except OSError:
                pass

    orchestrator.mux = mux_bound
    try:
        yield
    finally:
        orchestrator.mux = original_mux


def install_m10_live_binding() -> None:
    current = orchestrator.produce
    if getattr(current, "_isco_m10_live_binding", False):
        return

    def wrapped(*args, **kwargs):
        with m10_live_scope():
            return current(*args, **kwargs)

    wrapped._isco_m10_live_binding = True
    wrapped._isco_m10_original = current
    orchestrator.produce = wrapped

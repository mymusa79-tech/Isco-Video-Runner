from __future__ import annotations

"""Canonical text-representation seam for Long and standalone Moment production.

Run 179 exposed a boundary bug rather than a quality-policy bug: standalone Moment
plans intentionally keep ``narration`` empty and carry viewer-facing copy in
``on_screen_text``, while several generic Engine audits still reasoned as if narration
were authoritative for every format. Runner owns the Long-vs-Short composition seam,
so it also owns the adapter that tells generic audits which representation is valid.

Hard quality rules are not weakened here. Tone, dignity, naturalness, religious quote,
factuality and sensitive-topic blocks remain blocking. The only normalized verdict is
a short-template *format* complaint after the deterministic Runner-owned Short contract
has independently proved that the selected template is visibly represented.
"""

import re
from contextvars import ContextVar
from functools import wraps
from typing import Any, Callable

import isco_video_agent.orchestrator as orchestrator
import isco_video_agent.planner as engine_planner


_INSTALLED = False
_ACTIVE_PLAN: ContextVar[object | None] = ContextVar(
    "isco_production_text_representation_plan",
    default=None,
)

_TONE_FLAG_FIELDS = (
    "preachiness_flags",
    "cultural_dignity_flags",
    "naturalness_flags",
    "narrative_format_flags",
    "unverified_religious_quote_flags",
)

_WHY_REFRAME_MARKERS = (
    "لكن",
    "بل",
    "المشكلة ليست",
    "الأدق",
    "في الواقع",
    "الحقيقة أن",
    "بينما",
    "بدلا من",
    "بدلًا من",
    "أحيانا",
    "أحيانًا",
)
_MICRO_STORY_MARKERS = (
    "عندما",
    "حين",
    "ثم",
    "بعد",
    "قبل",
    "في لحظة",
    "بدأ",
    "بدأت",
    "حدث",
    "كانت",
    "كان",
)


class ProductionTextRepresentationContractError(RuntimeError):
    pass


def _clean(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def _format(plan: object) -> str:
    return _clean(getattr(plan, "format", "")).lower()


def _sections(plan: object) -> list[object]:
    return list(getattr(plan, "sections", []) or [])


def _short_template(plan: object) -> str:
    intent = getattr(plan, "editorial_intent", None)
    if isinstance(intent, dict):
        value = _clean(intent.get("short_template")).lower()
        if value:
            return value
    narrative = _clean(getattr(plan, "narrative_format", "")).lower()
    if narrative.startswith("short_"):
        return narrative.removeprefix("short_")
    return ""


def authoritative_section_text(plan: object, section: object) -> str:
    """Return the canonical human-facing text for a section.

    Long-form audio scripts are narration-authoritative. Standalone Moment is a silent
    screen-text format by contract, so ``on_screen_text`` is authoritative there.
    """
    if _format(plan) == "moment":
        return _clean(getattr(section, "on_screen_text", ""))
    return _clean(getattr(section, "narration", ""))


def authoritative_plan_text(plan: object) -> str:
    fields = [
        _clean(getattr(plan, "topic", "")),
        _clean(getattr(plan, "hook", "")),
        _clean(getattr(plan, "cta", "")),
        _clean(getattr(plan, "closing_payoff", "")),
    ]
    fields.extend(authoritative_section_text(plan, section) for section in _sections(plan))
    return "\n".join(value for value in fields if value)


def short_representation_issues(plan: object) -> list[str]:
    """Validate only the representation owned by the selected standalone Short template."""
    if _format(plan) != "moment":
        return []
    sections = _sections(plan)
    if len(sections) != 1:
        return ["moment_requires_exactly_one_section"]

    section = sections[0]
    if _clean(getattr(section, "narration", "")):
        return ["moment_narration_must_be_empty"]
    visible = authoritative_section_text(plan, section)
    if not visible:
        return ["moment_on_screen_text_missing"]

    template = _short_template(plan)
    if template == "inner_dialogue":
        # Accept the two natural forms our Short contract can emit: turn punctuation
        # (— … — …) or two or more visibly quoted inner thoughts. Do not require named
        # speakers because an inner dialogue is intentionally one person's cognition.
        dash_turns = len(re.findall(r"(?:^|\s)[—–-]\s*\S+", visible))
        arabic_quote_turns = min(visible.count("«"), visible.count("»"))
        if dash_turns < 2 and arabic_quote_turns < 2:
            return ["inner_dialogue_missing_visible_exchange"]
    elif template == "why_reframe":
        combined = authoritative_plan_text(plan)
        if not any(marker in combined for marker in _WHY_REFRAME_MARKERS):
            return ["why_reframe_missing_explicit_contrast_or_reframe"]
    elif template == "micro_story":
        combined = authoritative_plan_text(plan)
        if not any(marker in combined for marker in _MICRO_STORY_MARKERS):
            return ["micro_story_missing_concrete_event_progression"]
    elif template == "quote_reflection":
        combined = authoritative_plan_text(plan)
        has_arabic_quote = "«" in combined and "»" in combined
        has_ascii_quote = len(re.findall(r'"[^"\n]{2,}"', combined)) >= 1
        if not (has_arabic_quote or has_ascii_quote):
            return ["quote_reflection_missing_visible_quote"]
    elif template:
        return ["unsupported_short_template_representation:" + template]

    return []


def normalize_tone_audit_for_plan(plan: object, result: dict[str, Any]) -> dict[str, Any]:
    """Resolve only a proven Short template-ownership false positive.

    Generic Tone QA still owns preachiness, dignity, naturalness and religious quote
    semantics. Runner's deterministic Short Stage/Producer contract owns whether the
    chosen ``short_*`` narrative template is structurally present. A provider cannot
    veto that already-proven structure by interpreting silent Moment narration as a
    long-form monologue.
    """
    normalized = dict(result)
    if _format(plan) != "moment":
        return normalized

    narrative_flags = list(normalized.get("narrative_format_flags") or [])
    if not narrative_flags or str(normalized.get("status") or "").lower() != "block":
        return normalized

    other_flags = []
    for field in _TONE_FLAG_FIELDS:
        if field == "narrative_format_flags":
            continue
        other_flags.extend(list(normalized.get(field) or []))
    if other_flags:
        return normalized

    issues = short_representation_issues(plan)
    if issues:
        normalized["short_representation_issues"] = issues
        return normalized

    normalized["ignored_out_of_scope_narrative_format_flags"] = narrative_flags
    normalized["narrative_format_flags"] = []
    normalized["narrative_format_authority"] = "runner_deterministic_short_contract"
    normalized["status"] = "pass"
    return normalized


def semantic_repetition_audit_for_plan(
    plan: object,
    original: Callable[..., dict[str, Any]],
    *args: Any,
    **kwargs: Any,
) -> dict[str, Any]:
    """Skip an inapplicable cross-section LLM audit for one-section Moment only."""
    if _format(plan) == "moment" and len(_sections(plan)) <= 1:
        return {
            "status": "pass",
            "duplicate_groups": [],
            "reason": "not_applicable_single_section_moment",
            "audit_scope": "deterministic_format_contract",
        }
    return original(*args, **kwargs)


def augment_sensitive_review_text(plan: object | None, text: str) -> str:
    if plan is None or _format(plan) != "moment":
        return text
    screen_text = "\n".join(
        authoritative_section_text(plan, section)
        for section in _sections(plan)
        if authoritative_section_text(plan, section)
    )
    if not screen_text:
        return text
    return f"{text}\n{screen_text}" if text else screen_text


def screen_religious_quote_marker(plan: object, markers: tuple[str, ...]) -> str | None:
    if _format(plan) != "moment":
        return None
    text = "\n".join(authoritative_section_text(plan, section) for section in _sections(plan))
    return next((marker for marker in markers if marker and marker in text), None)


def install_production_text_representation_contract() -> None:
    """Install the format-aware adapter after Producer lifecycle ownership is final."""
    global _INSTALLED
    if _INSTALLED:
        return

    current_build = orchestrator.build_plan
    if not getattr(current_build, "_isco_producer_planning_lifecycle", False):
        raise ProductionTextRepresentationContractError(
            "producer_planning_lifecycle_must_be_installed_first"
        )

    @wraps(current_build)
    def build_wrapped(*args: Any, **kwargs: Any):
        plan = current_build(*args, **kwargs)
        issues = short_representation_issues(plan)
        if issues:
            raise ProductionTextRepresentationContractError(
                "short_representation_contract_blocked:" + ",".join(issues)
            )
        _ACTIVE_PLAN.set(plan)
        return plan

    build_wrapped._isco_producer_quality_contract = True
    build_wrapped._isco_producer_quality_original = getattr(
        current_build,
        "_isco_producer_quality_original",
        current_build,
    )
    build_wrapped._isco_producer_planning_lifecycle = True
    build_wrapped._isco_production_text_representation_contract = True
    orchestrator.build_plan = build_wrapped

    current_content = orchestrator.audit_semantic_repetition

    @wraps(current_content)
    def content_wrapped(api_key: str, plan: object, model: str):
        return semantic_repetition_audit_for_plan(
            plan,
            current_content,
            api_key,
            plan,
            model,
        )

    content_wrapped._isco_production_text_representation_contract = True
    orchestrator.audit_semantic_repetition = content_wrapped

    current_tone = orchestrator.audit_tone_and_naturalness

    @wraps(current_tone)
    def tone_wrapped(api_key: str, plan: object, model: str):
        raw = current_tone(api_key, plan, model)
        return normalize_tone_audit_for_plan(plan, raw)

    tone_wrapped._isco_production_text_representation_contract = True
    orchestrator.audit_tone_and_naturalness = tone_wrapped

    current_sensitive = orchestrator.enforce_sensitive_topic_review

    @wraps(current_sensitive)
    def sensitive_wrapped(text: str, human_approved: bool):
        return current_sensitive(
            augment_sensitive_review_text(_ACTIVE_PLAN.get(), text),
            human_approved=human_approved,
        )

    sensitive_wrapped._isco_production_text_representation_contract = True
    orchestrator.enforce_sensitive_topic_review = sensitive_wrapped

    current_quote_guard = engine_planner._reject_unverified_religious_quotes
    markers = tuple(getattr(engine_planner, "_RELIGIOUS_QUOTE_MARKERS", ()))

    @wraps(current_quote_guard)
    def quote_guard_wrapped(plan: object) -> None:
        marker = screen_religious_quote_marker(plan, markers)
        if marker:
            raise RuntimeError(
                "Editorial gate blocked a direct religious quotation/attribution without verified-source approval: "
                + marker
            )
        current_quote_guard(plan)

    quote_guard_wrapped._isco_production_text_representation_contract = True
    engine_planner._reject_unverified_religious_quotes = quote_guard_wrapped

    _INSTALLED = True
    print(
        "Production text representation contract installed: "
        "Long=narration; Moment=on_screen_text; Short template ownership deterministic; "
        "all substantive quality/safety flags remain fail-closed"
    )

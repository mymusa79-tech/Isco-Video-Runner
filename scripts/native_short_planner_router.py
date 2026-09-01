from __future__ import annotations

import os
import re
from typing import Any

import isco_video_agent.orchestrator as orchestrator
import isco_video_agent.planner as native_short
import isco_video_agent.resilient_planner as resilient

from scripts.task_level_planner_router import install_router as install_task_router


class NativeShortPlannerError(RuntimeError):
    pass


_TEMPLATE_ORDER = (
    "why_reframe",
    "inner_dialogue",
    "micro_story",
    "quote_reflection",
)

_TEMPLATE_SIGNALS: dict[str, tuple[tuple[str, int], ...]] = {
    "why_reframe": (
        ("لماذا", 4),
        ("السبب", 3),
        ("المشكلة ليست", 5),
        ("الحقيقة", 3),
        ("في الحقيقة", 4),
        ("في الواقع", 3),
        ("ليس", 2),
        ("لكن", 1),
        ("بل", 2),
        ("تظن", 3),
        ("تعتقد", 3),
        ("خطأ", 3),
        ("وهم", 3),
        ("خرافة", 4),
        ("بدل", 2),
    ),
    "inner_dialogue": (
        ("قلت لنفسي", 6),
        ("أقول لنفسي", 6),
        ("سألت نفسي", 5),
        ("أسأل نفسي", 5),
        ("بيني وبين نفسي", 6),
        ("صوت داخلي", 5),
        ("في داخلي", 4),
        ("لا أستطيع", 5),
        ("لن أستطيع", 5),
        ("أخاف", 3),
        ("الخوف", 3),
        ("قلق", 3),
        ("متردد", 3),
        ("التردد", 3),
        ("الدافع", 3),
        ("فقد الدافع", 5),
        ("الثقة", 2),
        ("أشعر", 2),
        ("ماذا لو", 4),
    ),
    "micro_story": (
        ("قصة", 5),
        ("ذات يوم", 6),
        ("في يوم", 4),
        ("مرة", 3),
        ("عندما", 3),
        ("حين", 2),
        ("حدث", 4),
        ("بدأت", 3),
        ("قررت", 3),
        ("مررت", 3),
        ("تجربة", 3),
        ("في تلك اللحظة", 5),
        ("بعد ذلك", 3),
        ("ثم", 1),
    ),
    "quote_reflection": (
        ("اقتباس", 7),
        ("مقولة", 7),
        ("هذه العبارة", 6),
        ("تلك العبارة", 6),
        ("عبارة", 4),
        ("كما قال", 5),
        ("قال لي", 4),
        ("قالت لي", 4),
    ),
}

_TEMPLATE_COMPENSATION = {
    "why_reframe": {
        "beat_shape": ["hook_misbelief", "contrast", "reframe", "payoff_action"],
        "visual_rhythm": "contrast_reframe",
        "voice_recommendation": "text_led_or_hybrid",
    },
    "inner_dialogue": {
        "beat_shape": ["inner_voice", "friction", "turn", "payoff_action"],
        "visual_rhythm": "intimate_pressure_release",
        "voice_recommendation": "hybrid_or_voice_led",
    },
    "micro_story": {
        "beat_shape": ["scene_hook", "event", "turn", "meaning_payoff"],
        "visual_rhythm": "micro_narrative_progression",
        "voice_recommendation": "voice_led_or_hybrid",
    },
    "quote_reflection": {
        "beat_shape": ["quote_hook", "pause", "reflection", "payoff"],
        "visual_rhythm": "held_frame_then_release",
        "voice_recommendation": "text_led_or_hybrid",
    },
}

_TEMPLATE_WRITING_DIRECTIVES = {
    "why_reframe": (
        "Standalone Short type is why_reframe. Write the Moment around one specific mistaken assumption: "
        "hook the misconception immediately, contrast it with the useful truth, reframe it, then end with one concrete action. "
        "Keep every text beat short, natural Modern Standard Arabic; do not add generic motivation."
    ),
    "inner_dialogue": (
        "Standalone Short type is inner_dialogue. Write the Moment as a compact internal tension: an immediate inner-voice hook, "
        "the friction it creates, a clear turn in perspective, then one practical payoff/action. Keep it natural Modern Standard "
        "Arabic, intimate but not melodramatic, and do not fabricate autobiography."
    ),
    "micro_story": (
        "Standalone Short type is micro_story. Write the Moment as a tiny concrete progression: enter an immediate scene/event, "
        "show what changes, make one clear turn, then land the meaning/payoff. Do not invent personal facts; use a generic human "
        "scenario unless the approved topic itself supplies a real event."
    ),
    "quote_reflection": (
        "Standalone Short type is quote_reflection. Use only the actual quotation present in the approved topic as the hook; never "
        "invent, alter, or attribute a quote. Follow it with a brief reflection and a concrete payoff. If the topic does not contain "
        "a real quote, this template must not be used."
    ),
}


def _clean(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def _semantic_key(value: object) -> str:
    text = _clean(value).casefold()
    text = re.sub(r"[\u064b-\u065f\u0670\u0640]", "", text)
    text = text.translate(str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي", "ة": "ه"}))
    return " ".join(re.sub(r"[^\w\u0600-\u06ff]+", " ", text).split())


def _signal_score(text: object, signals: tuple[tuple[str, int], ...]) -> int:
    normalized = f" {_semantic_key(text)} "
    return sum(weight for phrase, weight in signals if f" {_semantic_key(phrase)} " in normalized)


def _paired_quote(text: object) -> bool:
    raw = _clean(text)
    return any(opening in raw and closing in raw for opening, closing in (("«", "»"), ("“", "”"))) or raw.count('"') >= 2


def _plan_support_text(plan: object | None) -> str:
    if plan is None:
        return ""
    values: list[str] = [
        _clean(getattr(plan, "hook", "")),
        _clean(getattr(plan, "closing_payoff", "")),
    ]
    for item in list(getattr(plan, "title_options", []) or [])[:2]:
        values.append(_clean(item))
    sections = list(getattr(plan, "sections", []) or [])
    if sections:
        first = sections[0]
        values.extend(
            [
                _clean(getattr(first, "on_screen_text", "")),
                _clean(getattr(first, "key_point", "")),
                _clean(getattr(first, "narration", "")),
            ]
        )
    return " ".join(value for value in values if value)


def select_native_short_template(topic: object, plan: object | None = None) -> dict[str, Any]:
    """Choose standalone Short type from the approved topic before any content writing.

    Generated plan text is retained only as post-write diagnostics; it can never change
    the selected type. This prevents the model's wording from steering its own template.
    No extra provider call is made. quote_reflection remains fail-closed unless the
    approved topic itself contains quote evidence.
    """
    topic_text = _clean(topic)
    topic_scores = {
        template: _signal_score(topic_text, _TEMPLATE_SIGNALS[template])
        for template in _TEMPLATE_ORDER
    }
    topic_key = _semantic_key(topic_text)
    if " كيف " in f" {topic_key} ":
        topic_scores["inner_dialogue"] += 2
    if " لماذا " in f" {topic_key} ":
        topic_scores["why_reframe"] += 3

    paired_quote_evidence = _paired_quote(topic_text)
    quote_signal_score = _signal_score(topic_text, _TEMPLATE_SIGNALS["quote_reflection"])
    quote_evidence = paired_quote_evidence or quote_signal_score > 0
    if paired_quote_evidence and topic_scores["quote_reflection"] <= 0:
        # Delimited quote text is itself positive topic evidence. Give it only a
        # minimal score so stronger explicit story/reframe/dialogue signals can
        # still win, while preventing the unrelated pillar fallback from
        # overriding an otherwise unambiguous quote-only topic.
        topic_scores["quote_reflection"] = 1
    if not quote_evidence:
        topic_scores["quote_reflection"] = -100

    fallback_pillar = ""
    if max(topic_scores.values()) <= 0:
        try:
            fallback_pillar = _clean(native_short.choose_pillar(topic_text))
        except Exception:
            fallback_pillar = ""
        fallback = {
            "understand": "why_reframe",
            "rise": "inner_dialogue",
            "see": "micro_story",
        }.get(fallback_pillar, "why_reframe")
        topic_scores[fallback] = 1

    best = max(topic_scores.values())
    template = next(item for item in _TEMPLATE_ORDER if topic_scores[item] == best)
    support_text = _plan_support_text(plan)
    support_scores = {
        item: _signal_score(support_text, _TEMPLATE_SIGNALS[item])
        for item in _TEMPLATE_ORDER
    }
    return {
        "template": template,
        "scores": topic_scores,
        "support_scores_non_authoritative": support_scores,
        "selection_basis": "approved_topic_only_before_writing",
        "fallback_pillar": fallback_pillar or None,
        "extra_ai_calls": 0,
        "quote_evidence": quote_evidence,
    }


def merge_short_template_revision(template: str, existing: object) -> str:
    """Compose the selected Short template with all earlier writing requirements."""
    directive = _TEMPLATE_WRITING_DIRECTIVES[template]
    previous = _clean(existing)
    if previous:
        return f"{directive} Additional revision requirement: {previous}"
    return directive


# Backward-compatible private name for already-installed callers.
_planning_revision_note = merge_short_template_revision


def _attach_compensation_metadata(
    plan: object,
    topic: object,
    preselected: dict[str, Any],
) -> dict[str, Any]:
    postwrite = select_native_short_template(topic, plan)
    if postwrite["template"] != preselected["template"]:
        raise NativeShortPlannerError("native_short_template_changed_after_topic_preselection")
    selection = dict(preselected)
    selection["support_scores_non_authoritative"] = postwrite["support_scores_non_authoritative"]
    template = selection["template"]
    current = getattr(plan, "editorial_intent", None)
    intent = dict(current) if isinstance(current, dict) else {}
    intent["short_template"] = template
    intent["short_template_selection"] = selection
    intent["short_compensation_v2"] = {
        "enabled": True,
        "scope": "short_only",
        "template": template,
        **_TEMPLATE_COMPENSATION[template],
        "type_selected_before_writing": True,
        "writing_directive_applied": True,
        "beat_driven_text": True,
        "beat_driven_visual_reframe": True,
        "extra_ai_calls": 0,
    }
    setattr(plan, "editorial_intent", intent)
    setattr(plan, "narrative_format", f"short_{template}")
    return selection


def install_native_short_router() -> None:
    """Install a moment-capable planner while reusing the existing provider mesh.

    task_level_planner_router installs the vetted Gemini/Groq/OpenRouter JSON router and
    channel persona at the provider boundary. The long-form resilient planner cannot
    accept format=moment, so this adapter reuses only that provider router and delegates
    the moment schema to Engine's native planner. The approved topic selects the Short
    type before the content-model call, and that type actively directs the writing and
    later compensation without spending an extra AI call.
    """
    install_task_router()
    routed_json_text = resilient.json_text
    native_short.json_text = routed_json_text

    def routed_build_plan(api_key, topic, requested_format, content_model, **kwargs):
        if str(requested_format or "").strip().lower() != "moment":
            raise NativeShortPlannerError("native_short_router_requires_moment")
        preselected = select_native_short_template(topic)
        template = str(preselected["template"])
        plan = native_short.build_plan(
            api_key,
            topic,
            "moment",
            content_model,
            research_context=kwargs.get("research_context"),
            avoid_context=kwargs.get("avoid_context"),
            revision_note=merge_short_template_revision(
                template,
                kwargs.get("revision_note", ""),
            ),
            allow_fallback=False,
        )
        if getattr(plan, "format", None) != "moment":
            raise NativeShortPlannerError("native_short_router_returned_non_moment_plan")
        selection = _attach_compensation_metadata(plan, topic, preselected)
        os.environ["ISCO_NATIVE_SHORT_TEMPLATE"] = str(selection["template"])
        os.environ.pop("ISCO_DIALOGUE_QA", None)
        return plan

    routed_build_plan._is_resilient_router = True
    orchestrator.build_plan = routed_build_plan

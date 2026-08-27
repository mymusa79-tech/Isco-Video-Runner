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


def _plan_support_text(plan: object) -> str:
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


def select_native_short_template(topic: object, plan: object) -> dict[str, Any]:
    """Choose the standalone Short type from topic meaning, with plan text only as support.

    The approved topic is intentionally weighted 3x so the generated wording cannot
    silently steer the Short into a different series type. No extra provider call is
    made. quote_reflection remains fail-closed unless quote evidence exists.
    """
    topic_text = _clean(topic)
    support_text = _plan_support_text(plan)
    scores: dict[str, int] = {}
    for template in _TEMPLATE_ORDER:
        topic_score = _signal_score(topic_text, _TEMPLATE_SIGNALS[template])
        support_score = _signal_score(support_text, _TEMPLATE_SIGNALS[template])
        scores[template] = topic_score * 3 + support_score

    topic_key = _semantic_key(topic_text)
    if " كيف " in f" {topic_key} ":
        scores["inner_dialogue"] += 2
    if " لماذا " in f" {topic_key} ":
        scores["why_reframe"] += 3

    quote_evidence = (
        _paired_quote(topic_text)
        or _paired_quote(support_text)
        or _signal_score(topic_text, _TEMPLATE_SIGNALS["quote_reflection"]) > 0
    )
    if not quote_evidence:
        scores["quote_reflection"] = -100

    if max(scores.values()) <= 0:
        pillar = _clean(getattr(plan, "pillar", ""))
        fallback = {
            "understand": "why_reframe",
            "rise": "inner_dialogue",
            "see": "micro_story",
        }.get(pillar, "why_reframe")
        scores[fallback] = 1

    best = max(scores.values())
    template = next(item for item in _TEMPLATE_ORDER if scores[item] == best)
    return {
        "template": template,
        "scores": scores,
        "selection_basis": "approved_topic_primary_plan_support_secondary",
        "topic_weight": 3,
        "extra_ai_calls": 0,
        "quote_evidence": quote_evidence,
    }


def _attach_compensation_metadata(plan: object, topic: object) -> dict[str, Any]:
    selection = select_native_short_template(topic, plan)
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
    the moment schema to Engine's native planner. No extra provider family is introduced.
    Standalone Shorts then receive a deterministic topic-led template and compensation
    contract without spending another AI call.
    """
    install_task_router()
    routed_json_text = resilient.json_text
    native_short.json_text = routed_json_text

    def routed_build_plan(api_key, topic, requested_format, content_model, **kwargs):
        if str(requested_format or "").strip().lower() != "moment":
            raise NativeShortPlannerError("native_short_router_requires_moment")
        plan = native_short.build_plan(
            api_key,
            topic,
            "moment",
            content_model,
            research_context=kwargs.get("research_context"),
            avoid_context=kwargs.get("avoid_context"),
            revision_note=kwargs.get("revision_note", ""),
            allow_fallback=False,
        )
        if getattr(plan, "format", None) != "moment":
            raise NativeShortPlannerError("native_short_router_returned_non_moment_plan")
        selection = _attach_compensation_metadata(plan, topic)
        os.environ["ISCO_NATIVE_SHORT_TEMPLATE"] = str(selection["template"])
        os.environ.pop("ISCO_DIALOGUE_QA", None)
        return plan

    routed_build_plan._is_resilient_router = True
    orchestrator.build_plan = routed_build_plan

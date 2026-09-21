from __future__ import annotations

import re
from typing import Any, Mapping

SHORT_SECTION_COUNT = 3
SHORT_WIDTH = 1080
SHORT_HEIGHT = 1920
SHORT_TARGET_SECONDS = 75.0
SHORT_MIN_SECONDS = 60.0
SHORT_MAX_SECONDS = 90.0

TEMPLATE_ORDER = (
    "why_reframe",
    "inner_dialogue",
    "micro_story",
    "quote_reflection",
)

TEMPLATE_SIGNALS: dict[str, tuple[tuple[str, int], ...]] = {
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

TEMPLATE_COMPENSATION = {
    "why_reframe": {
        "beat_shape": ["hook_misbelief", "contrast", "reframe_payoff"],
        "visual_rhythm": "contrast_reframe",
    },
    "inner_dialogue": {
        "beat_shape": ["inner_voice_hook", "friction_turn", "payoff_action"],
        "visual_rhythm": "intimate_pressure_release",
    },
    "micro_story": {
        "beat_shape": ["scene_hook", "event_turn", "meaning_payoff"],
        "visual_rhythm": "micro_narrative_progression",
    },
    "quote_reflection": {
        "beat_shape": ["quote_hook", "reflection", "payoff"],
        "visual_rhythm": "held_frame_then_release",
    },
}

TEMPLATE_WRITING_DIRECTIVES = {
    "why_reframe": (
        "Short type is why_reframe. Open on one specific mistaken assumption, contrast it with the "
        "useful truth, reframe it, then land one concrete payoff/action. Keep the Arabic natural and "
        "specific; do not add generic motivation."
    ),
    "inner_dialogue": (
        "Short type is inner_dialogue. Open with an immediate internal-tension line, show the friction, "
        "turn the perspective, then land one practical payoff/action. Keep it intimate but not melodramatic "
        "and never fabricate autobiography."
    ),
    "micro_story": (
        "Short type is micro_story. Enter a tiny concrete scene immediately, show one event/turn, then land "
        "the meaning/payoff. Do not invent personal facts; use a generic human scenario unless the approved "
        "brief itself supplies a real event."
    ),
    "quote_reflection": (
        "Short type is quote_reflection. Use only an actual quotation explicitly present in the approved "
        "topic as the opening hook; never invent, alter, or attribute a quote. Follow with a brief reflection "
        "and a concrete payoff."
    ),
}


class ShortFormatError(RuntimeError):
    pass


def _clean(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def _semantic_key(value: object) -> str:
    text = _clean(value).casefold()
    text = re.sub(r"[\u064b-\u065f\u0670\u0640]", "", text)
    text = text.translate(str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي", "ة": "ه"}))
    return " ".join(re.sub(r"[^\w\u0600-\u06ff]+", " ", text).split())


def _signal_score(text: object, signals: tuple[tuple[str, int], ...]) -> int:
    normalized = f" {_semantic_key(text)} "
    return sum(
        weight
        for phrase, weight in signals
        if f" {_semantic_key(phrase)} " in normalized
    )


def _paired_quote(text: object) -> bool:
    raw = _clean(text)
    return any(
        opening in raw and closing in raw
        for opening, closing in (("«", "»"), ("“", "”"))
    ) or raw.count('"') >= 2


def _research_text(brief: Mapping[str, Any]) -> str:
    values: list[str] = []
    pack = brief.get("research_pack")
    if isinstance(pack, list):
        for row in pack:
            if not isinstance(row, Mapping):
                continue
            values.append(_clean(row.get("source_title")))
            values.append(_clean(row.get("claim_scope")))
    return " ".join(value for value in values if value)


def _emotional_goal_text(brief: Mapping[str, Any]) -> str:
    return " ".join(
        value
        for value in (
            _clean(brief.get("emotional_goal")),
            _clean(brief.get("emotional_arc")),
            _clean(brief.get("editorial_intent")),
        )
        if value
    )


def select_short_template(brief: Mapping[str, Any]) -> dict[str, Any]:
    """Choose one of the four frozen Short templates without another AI call.

    The approved topic is authoritative and receives the strongest weight. Approved
    evidence and the declared emotional/editorial goal only refine ties and weak
    signals; generated plan/script text is never allowed to steer its own template.
    """
    topic = _clean(brief.get("approved_topic"))
    if not topic:
        raise ShortFormatError("short_template_requires_approved_topic")

    evidence = _research_text(brief)
    emotional_goal = _emotional_goal_text(brief)
    scores: dict[str, int] = {}
    for template in TEMPLATE_ORDER:
        signals = TEMPLATE_SIGNALS[template]
        scores[template] = (
            3 * _signal_score(topic, signals)
            + _signal_score(evidence, signals)
            + 2 * _signal_score(emotional_goal, signals)
        )

    topic_key = f" {_semantic_key(topic)} "
    if " لماذا " in topic_key:
        scores["why_reframe"] += 4
    if " كيف " in topic_key:
        scores["inner_dialogue"] += 2

    quote_evidence = _paired_quote(topic)
    if not quote_evidence:
        scores["quote_reflection"] = -100

    if max(scores.values()) <= 0:
        pillar = _clean(brief.get("pillar")).lower()
        fallback = {
            "understand": "why_reframe",
            "rise": "inner_dialogue",
            "see": "micro_story",
        }.get(pillar, "why_reframe")
        scores[fallback] = 1

    best = max(scores.values())
    template = next(item for item in TEMPLATE_ORDER if scores[item] == best)
    if template == "quote_reflection" and not quote_evidence:
        raise ShortFormatError("quote_reflection_requires_explicit_quote")

    return {
        "schema_version": 1,
        "template": template,
        "scores": scores,
        "selection_basis": "approved_topic_plus_evidence_plus_emotional_goal",
        "quote_evidence": quote_evidence,
        "beat_shape": TEMPLATE_COMPENSATION[template]["beat_shape"],
        "visual_rhythm": TEMPLATE_COMPENSATION[template]["visual_rhythm"],
        "writing_directive": TEMPLATE_WRITING_DIRECTIVES[template],
        "extra_ai_calls": 0,
    }


def short_prompt_context(brief: Mapping[str, Any]) -> str:
    selection = select_short_template(brief)
    beats = " -> ".join(selection["beat_shape"])
    return (
        "SHORT_FORMAT_CONTRACT:\n"
        f"- selected_template={selection['template']}\n"
        f"- beat_shape={beats}\n"
        f"- target_duration_seconds={SHORT_TARGET_SECONDS:g}; hard_range="
        f"{SHORT_MIN_SECONDS:g}-{SHORT_MAX_SECONDS:g}\n"
        f"- exact_sections={SHORT_SECTION_COUNT}; frame={SHORT_WIDTH}x{SHORT_HEIGHT}\n"
        "- The first spoken sentence is the hook. No greeting, channel identity opener, dialogue labels, "
        "CTA, or quotation unless the selected quote_reflection template has explicit approved quote evidence.\n"
        f"- {selection['writing_directive']}"
    )


def validate_short_duration(seconds: float, *, phase: str) -> float:
    value = float(seconds)
    if not SHORT_MIN_SECONDS <= value <= SHORT_MAX_SECONDS:
        raise ShortFormatError(
            "short_duration_out_of_range "
            f"phase={phase} seconds={value:.3f} "
            f"allowed={SHORT_MIN_SECONDS:g}-{SHORT_MAX_SECONDS:g}"
        )
    return value


def validate_short_dimensions(width: int, height: int) -> tuple[int, int]:
    actual = (int(width), int(height))
    expected = (SHORT_WIDTH, SHORT_HEIGHT)
    if actual != expected:
        raise ShortFormatError(
            f"short_frame_mismatch actual={actual[0]}x{actual[1]} "
            f"expected={expected[0]}x{expected[1]}"
        )
    return actual

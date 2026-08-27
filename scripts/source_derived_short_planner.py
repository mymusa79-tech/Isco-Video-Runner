from __future__ import annotations

import hashlib
import re
from typing import Any

import isco_video_agent.orchestrator as orchestrator
from isco_video_agent.models import ProductionPlan, ScriptSection
from isco_video_agent.short_planner import build_short_plan, plan_as_dict


class SourceDerivedShortError(RuntimeError):
    pass


_TEMPLATE_ORDER = (
    "why_reframe",
    "inner_dialogue",
    "micro_story",
    "quote_reflection",
)
_DISQUALIFIED_SCORE = -100

# Source-text signals keep selection reproducible when the stored blueprint is
# recomputed before production. Higher weights represent template-defining
# evidence; the pillar remains only a weak contextual prior.
_WHY_REFRAME_SIGNALS = (
    ("المشكلة ليست", 4),
    ("لا يعني", 4),
    ("الحقيقة أن", 3),
    ("في الحقيقة", 3),
    ("في الواقع", 3),
    ("ما ينقصك", 3),
    ("خرافة", 3),
    ("اعتقاد", 2),
    ("بدلًا من", 2),
    ("أعد النظر", 2),
    ("تظن", 2),
    ("لماذا", 1),
    ("يبدو", 1),
    ("ليس", 1),
    ("لكن", 1),
    ("بل", 1),
)

_INNER_DIALOGUE_SIGNALS = (
    ("قلت لنفسي", 5),
    ("أقول لنفسي", 5),
    ("بيني وبين نفسي", 5),
    ("سألت نفسي", 4),
    ("أسأل نفسي", 4),
    ("صوتي الداخلي", 4),
    ("صوت داخلي", 4),
    ("حوار داخلي", 4),
    ("في داخلي", 3),
    ("ماذا لو", 3),
    ("لا أستطيع", 3),
    ("لن أستطيع", 3),
    ("أنا خائف", 2),
    ("أنا متردد", 2),
    ("أريد أن", 1),
    ("أشعر أن", 1),
    ("أظن أن", 1),
)

_MICRO_STORY_SIGNALS = (
    ("ذات يوم", 4),
    ("في تلك اللحظة", 3),
    ("في يوم", 3),
    ("قصة", 3),
    ("بعد ذلك", 2),
    ("كنت", 2),
    ("حدث", 2),
    ("قررت", 2),
    ("بدأت", 2),
    ("مررت", 2),
    ("تذكرت", 2),
    ("عندما", 1),
    ("حين", 1),
    ("ثم", 1),
    ("لاحقًا", 1),
    ("أخيرًا", 1),
)

_QUOTE_REFLECTION_SIGNALS = (
    ("اقتباس", 5),
    ("مقولة", 5),
    ("هذه العبارة", 4),
    ("تلك العبارة", 4),
    ("جملة قالها", 4),
    ("قال لي", 3),
    ("قالت لي", 3),
    ("كما قال", 3),
    ("عبارة", 2),
)


def _clean(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def _semantic_key(value: object) -> str:
    text = _clean(value).casefold()
    text = re.sub(r"[\u064b-\u065f\u0670\u0640]", "", text)
    text = text.translate(str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي", "ة": "ه"}))
    return " ".join(re.sub(r"[^\w\u0600-\u06ff]+", " ", text).split())


def _signal_score(text: str, signals: tuple[tuple[str, int], ...]) -> int:
    padded = f" {_semantic_key(text)} "
    return sum(
        weight
        for phrase, weight in signals
        if f" {_semantic_key(phrase)} " in padded
    )


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _compact_words(value: object, maximum: int = 14) -> str:
    words = _clean(value).split()
    return " ".join(words[:maximum])


def _sentences(value: object) -> list[str]:
    text = _clean(value)
    if not text:
        return []
    parts = [part.strip() for part in re.split(r"(?<=[.!؟!])\s+", text) if part.strip()]
    return parts or [text]


def _distinct_source_texts(excerpt: dict[str, Any]) -> list[str]:
    narration = _clean(excerpt.get("source_narration"))
    expected_narration_sha = _clean(excerpt.get("source_narration_sha256"))
    if not narration or len(expected_narration_sha) != 64 or _hash_text(narration) != expected_narration_sha:
        raise SourceDerivedShortError("source_narration_integrity_failed")

    sentences = _sentences(narration)
    values = [
        _compact_words(excerpt.get("source_on_screen_text"), 10),
        _compact_words(excerpt.get("source_key_point"), 14),
        _compact_words(sentences[0] if sentences else "", 14),
        _compact_words(sentences[-1] if sentences else "", 14),
    ]
    accepted: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _clean(value)
        key = re.sub(r"[^\w\u0600-\u06ff]+", " ", text.casefold()).strip()
        if not text or not key or key in seen:
            continue
        seen.add(key)
        accepted.append(text)
        if len(accepted) == 4:
            break
    if len(accepted) < 2:
        raise SourceDerivedShortError("source_section_has_insufficient_short_beats")
    return accepted


def _sibling_index(control_request: dict[str, Any]) -> int:
    raw_index = control_request.get("sibling_index")
    if isinstance(raw_index, bool):
        return 1
    if isinstance(raw_index, int):
        return raw_index if raw_index > 0 else 1
    if isinstance(raw_index, str) and re.fullmatch(r"[1-9]\d*", raw_index.strip()):
        return int(raw_index.strip())
    return 1


def _template_scores(control_request: dict[str, Any]) -> dict[str, int]:
    excerpt = control_request.get("source_episode_excerpt")
    if not isinstance(excerpt, dict):
        excerpt = {}
    topic_text = " ".join(
        [
            _clean(control_request.get("source_semantic_job")),
            _clean(excerpt.get("source_key_point")),
            _clean(excerpt.get("source_on_screen_text")),
            _clean(excerpt.get("source_narration")),
        ]
    )
    scores = {
        "why_reframe": _signal_score(topic_text, _WHY_REFRAME_SIGNALS),
        "inner_dialogue": _signal_score(topic_text, _INNER_DIALOGUE_SIGNALS),
        "micro_story": _signal_score(topic_text, _MICRO_STORY_SIGNALS),
        "quote_reflection": _DISQUALIFIED_SCORE,
    }

    pillar = _clean((control_request.get("candidate") or {}).get("pillar"))
    pillar_prior = {
        "understand": "why_reframe",
        "rise": "inner_dialogue",
        "see": "micro_story",
    }.get(pillar)
    if pillar_prior:
        scores[pillar_prior] += 1

    raw_topic_text = _clean(topic_text)
    paired_quote = any(
        opening in raw_topic_text and closing in raw_topic_text
        for opening, closing in (("«", "»"), ("“", "”"))
    ) or raw_topic_text.count('"') >= 2
    quote_signal_score = _signal_score(topic_text, _QUOTE_REFLECTION_SIGNALS)
    if paired_quote or quote_signal_score:
        scores["quote_reflection"] = 4 + quote_signal_score + (3 if paired_quote else 0)
    return scores


def _select_template(control_request: dict[str, Any]) -> str:
    """Select by source-topic fit; use sibling order only to break score ties."""
    scores = _template_scores(control_request)
    best_score = max(scores.values())
    offset = (_sibling_index(control_request) - 1) % len(_TEMPLATE_ORDER)
    tie_break_order = _TEMPLATE_ORDER[offset:] + _TEMPLATE_ORDER[:offset]
    return next(template for template in tie_break_order if scores[template] == best_score)


def build_source_short_blueprint(control_request: dict[str, Any]) -> dict[str, Any]:
    if control_request.get("kind") != "short":
        raise SourceDerivedShortError("source_short_request_kind_invalid")
    if control_request.get("approval_scope") != "short_sibling":
        raise SourceDerivedShortError("source_short_requires_sibling_scope")
    if control_request.get("approval_inherited_from_parent_bundle") is not True:
        raise SourceDerivedShortError("source_short_parent_approval_missing")
    if control_request.get("production_dispatch_authorized") is not False:
        raise SourceDerivedShortError("stored_source_short_must_be_non_dispatching")

    excerpt = control_request.get("source_episode_excerpt")
    if not isinstance(excerpt, dict):
        raise SourceDerivedShortError("source_episode_excerpt_missing")
    semantic_job = _clean(control_request.get("source_semantic_job"))
    if not semantic_job or semantic_job != _clean(excerpt.get("source_key_point")):
        raise SourceDerivedShortError("source_semantic_job_mismatch")
    visual_query = _clean(excerpt.get("source_visual_query"))
    if not visual_query:
        raise SourceDerivedShortError("source_visual_query_missing")

    texts = _distinct_source_texts(excerpt)
    action = _clean((control_request.get("short_admission") or {}).get("single_action_contract"))
    template = _select_template(control_request)
    source_id = ":".join(
        [
            _clean(control_request.get("parent_control_request_id")),
            _clean(control_request.get("source_production_plan_sha256")),
            _clean(excerpt.get("source_section_id")),
        ]
    )
    beats = [
        {
            "beat_id": f"b{index:02d}",
            "semantic_job": "hook" if index == 1 else ("payoff" if index == len(texts) else "development"),
            "text": text,
        }
        for index, text in enumerate(texts, 1)
    ]
    plan = build_short_plan(
        source_id=source_id,
        semantic_job=semantic_job,
        single_action_contract=action,
        template=template,
        beats=beats,
        source_kind="long_episode",
    )
    return plan_as_dict(plan)


def _validated_blueprint(control_request: dict[str, Any]) -> dict[str, Any]:
    expected = build_source_short_blueprint(control_request)
    stored = control_request.get("source_short_plan")
    if not isinstance(stored, dict) or stored != expected:
        raise SourceDerivedShortError("source_short_blueprint_changed_after_parent_derivation")
    return expected


def build_production_plan(control_request: dict[str, Any]) -> ProductionPlan:
    blueprint = _validated_blueprint(control_request)
    excerpt = control_request["source_episode_excerpt"]
    beat_texts = [_clean(item.get("text")) for item in blueprint["beats"]]
    if len(beat_texts) < 2:
        raise SourceDerivedShortError("source_short_blueprint_has_insufficient_beats")
    semantic_job = _clean(blueprint["semantic_job"])
    pillar = _clean((control_request.get("candidate") or {}).get("pillar"))
    if pillar not in {"understand", "rise", "see"}:
        pillar = "understand"

    section = ScriptSection(
        id="s1",
        narration="",
        visual_query=_clean(excerpt.get("source_visual_query"))[:260],
        on_screen_text=beat_texts[1][:220] if len(beat_texts) > 2 else beat_texts[0][:220],
        emotion=_clean(excerpt.get("source_emotion"))[:40] or "reflective",
        expected_seconds=15.0,
        key_point=semantic_job[:220],
    )
    titles = [semantic_job, beat_texts[0], beat_texts[-1]]
    unique_titles: list[str] = []
    seen: set[str] = set()
    for title in titles:
        key = _clean(title).casefold()
        if key and key not in seen:
            seen.add(key)
            unique_titles.append(_clean(title)[:220])
    while len(unique_titles) < 3:
        unique_titles.append(f"{semantic_job[:180]} — {len(unique_titles) + 1}")

    return ProductionPlan(
        topic=semantic_job,
        pillar=pillar,
        format="moment",
        hook=beat_texts[0][:300],
        title_options=unique_titles[:3],
        thumbnail_concepts=["vertical real-footage frame derived from the approved long episode visual language"],
        sections=[section],
        cta="",
        closing_payoff=beat_texts[-1][:300],
        editorial_intent={},
    )


def install_source_derived_short_planner(control_request: dict[str, Any]) -> None:
    expected_topic = _clean(control_request.get("approved_topic"))
    _validated_blueprint(control_request)

    def routed_build_plan(_api_key, topic, requested_format, _content_model, **_kwargs):
        if _clean(topic) != expected_topic:
            raise SourceDerivedShortError("source_short_topic_changed_after_approval")
        if _clean(requested_format).lower() != "moment":
            raise SourceDerivedShortError("source_short_format_must_be_moment")
        return build_production_plan(control_request)

    routed_build_plan._is_resilient_router = True
    orchestrator.build_plan = routed_build_plan

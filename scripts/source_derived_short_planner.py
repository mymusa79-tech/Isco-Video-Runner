from __future__ import annotations

import hashlib
import re
from typing import Any

import isco_video_agent.orchestrator as orchestrator
from isco_video_agent.models import ProductionPlan, ScriptSection
from isco_video_agent.short_planner import build_short_plan, plan_as_dict


class SourceDerivedShortError(RuntimeError):
    pass


def _clean(value: object) -> str:
    return " ".join(str(value or "").strip().split())


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
    template = "micro_story" if _clean((control_request.get("candidate") or {}).get("pillar")) == "see" else "why_reframe"
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

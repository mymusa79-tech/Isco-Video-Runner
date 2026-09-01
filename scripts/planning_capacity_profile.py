from __future__ import annotations

from typing import Any

from scripts import planning_capacity_headroom as headroom
from scripts import run125_capacity_routing_closure as run125


# Format-native payload caps. These are content-shape bounds, not provider quota
# overrides: they keep a 7-20s Moment from carrying Film-sized policy/research/output
# fields into a provider request while preserving every hard cultural/factuality rule.
# The caps include room for the channel persona that is injected after these payloads
# are built; the certified worst-case review must remain inside the 16KB routed prompt
# envelope instead of merely keeping the pre-persona JSON small.
SHORT_EFFECTIVE_PROMPT_MAX_UTF8_BYTES = 16_000
SHORT_MAX_REVISION_CHARS = 800
SHORT_MAX_RESEARCH_ITEMS = 2
SHORT_MAX_RESEARCH_VALUE_CHARS = 160
SHORT_MAX_BOUNDARY_ITEMS = 3
SHORT_MAX_BOUNDARY_CHARS = 160
SHORT_MAX_AVOID_ITEMS = 6
SHORT_MAX_AVOID_VALUE_CHARS = 180

_INSTALLED = False


def _text(value: object, limit: int) -> str:
    return " ".join(str(value or "").strip().split())[:limit]


def _bounded_avoid(value: object, *, depth: int = 0) -> Any:
    if depth >= 2:
        return _text(value, SHORT_MAX_AVOID_VALUE_CHARS)
    if isinstance(value, dict):
        return {
            _text(key, 70): _bounded_avoid(item, depth=depth + 1)
            for key, item in list(value.items())[:SHORT_MAX_AVOID_ITEMS]
        }
    if isinstance(value, list):
        return [
            _bounded_avoid(item, depth=depth + 1)
            for item in value[:SHORT_MAX_AVOID_ITEMS]
        ]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _text(value, SHORT_MAX_AVOID_VALUE_CHARS)


def compact_research_payload(context: dict | None) -> dict[str, Any]:
    source = context if isinstance(context, dict) else {}
    result: dict[str, Any] = {}

    for key in (
        "approved_audience",
        "approved_editorial_direction",
        "factuality_rule",
    ):
        value = source.get(key)
        if isinstance(value, list):
            result[key] = [
                _text(item, SHORT_MAX_BOUNDARY_CHARS)
                for item in value[:SHORT_MAX_BOUNDARY_ITEMS]
                if _text(item, SHORT_MAX_BOUNDARY_CHARS)
            ]
        elif value:
            result[key] = _text(value, SHORT_MAX_RESEARCH_VALUE_CHARS * 2)

    boundaries = source.get("content_boundaries")
    if isinstance(boundaries, list):
        result["content_boundaries"] = [
            _text(item, SHORT_MAX_BOUNDARY_CHARS)
            for item in boundaries[:SHORT_MAX_BOUNDARY_ITEMS]
            if _text(item, SHORT_MAX_BOUNDARY_CHARS)
        ]
    elif boundaries:
        result["content_boundaries"] = _text(
            boundaries, SHORT_MAX_RESEARCH_VALUE_CHARS * 2
        )

    pack = source.get("approved_research_pack")
    if isinstance(pack, list):
        compact_pack: list[Any] = []
        for item in pack[:SHORT_MAX_RESEARCH_ITEMS]:
            if isinstance(item, dict):
                kept: dict[str, str] = {}
                for key in ("title", "source", "publisher", "url", "claim", "evidence"):
                    if item.get(key):
                        kept[key] = _text(item.get(key), SHORT_MAX_RESEARCH_VALUE_CHARS)
                if kept:
                    compact_pack.append(kept)
            elif item:
                compact_pack.append(_text(item, SHORT_MAX_RESEARCH_VALUE_CHARS))
        if compact_pack:
            result["approved_research_pack"] = compact_pack
    return result


def compact_avoid_payload(context: dict | None) -> Any:
    return _bounded_avoid(context if isinstance(context, dict) else {})


def compact_plan_payload(plan: object) -> dict[str, Any]:
    sections = list(getattr(plan, "sections", []) or [])[:1]
    if not sections:
        raise headroom.PlanningCapacityHeadroomError(
            "Short review requires one existing Moment section"
        )
    section = sections[0]
    return {
        "topic": _text(getattr(plan, "topic", ""), 320),
        "pillar": _text(getattr(plan, "pillar", "understand"), 30),
        "format": "moment",
        "hook": _text(getattr(plan, "hook", ""), 220),
        "title_options": [
            _text(item, 100)
            for item in list(getattr(plan, "title_options", []) or [])[:3]
        ],
        "thumbnail_concepts": [
            _text(item, 140)
            for item in list(getattr(plan, "thumbnail_concepts", []) or [])[:3]
        ],
        "sections": [
            {
                "id": _text(getattr(section, "id", "s1"), 40) or "s1",
                "narration": "",
                "visual_query": _text(getattr(section, "visual_query", ""), 220),
                "on_screen_text": _text(getattr(section, "on_screen_text", ""), 160),
                "emotion": _text(getattr(section, "emotion", "reflective"), 40),
                "expected_seconds": max(
                    7.0,
                    min(20.0, float(getattr(section, "expected_seconds", 15.0) or 15.0)),
                ),
                "key_point": _text(getattr(section, "key_point", ""), 140),
            }
        ],
        "cta": _text(getattr(plan, "cta", ""), 180),
        "closing_payoff": _text(getattr(plan, "closing_payoff", ""), 220),
    }


def _install_headroom_model_failover_semantics() -> None:
    """Keep model-pool failover model-scoped when a request misses safety headroom.

    The two GPT-OSS models happen to share an 8K Free TPM ceiling today, but that is
    provider policy, not an architectural invariant. If one model's observed ceiling
    later differs, a local GROQ_TPM_CAPACITY_PREFLIGHT rejection must advance the model
    pool exactly like any other model-specific unavailability instead of silently
    assuming the next model has the same capacity.
    """
    if getattr(run125, "_ISCO_HEADROOM_MODEL_FAILOVER_V1", False):
        return
    original = run125._is_model_unavailable

    def model_unavailable(error) -> bool:
        return original(error) or "groq_tpm_capacity_preflight" in str(error).lower()

    run125._is_model_unavailable = model_unavailable
    run125._ISCO_HEADROOM_MODEL_FAILOVER_V1 = True


def install_planning_capacity_profile() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    headroom.SHORT_EFFECTIVE_PROMPT_MAX_UTF8_BYTES = SHORT_EFFECTIVE_PROMPT_MAX_UTF8_BYTES
    headroom.SHORT_MAX_REVISION_CHARS = SHORT_MAX_REVISION_CHARS
    headroom.SHORT_MAX_RESEARCH_ITEMS = SHORT_MAX_RESEARCH_ITEMS
    headroom.SHORT_MAX_RESEARCH_VALUE_CHARS = SHORT_MAX_RESEARCH_VALUE_CHARS
    headroom.SHORT_MAX_AVOID_ITEMS = SHORT_MAX_AVOID_ITEMS
    headroom._compact_research_payload = compact_research_payload
    headroom._compact_avoid_payload = compact_avoid_payload
    headroom._plan_payload = compact_plan_payload
    _install_headroom_model_failover_semantics()
    _INSTALLED = True

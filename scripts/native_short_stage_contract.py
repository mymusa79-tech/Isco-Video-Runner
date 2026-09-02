from __future__ import annotations

"""Explicit Stage Contract for standalone Moment Draft/Review/Repair calls.

The Engine Moment planner intentionally bypasses resilient_planner's long-form stage
functions, but it still uses the same provider mesh. Historically that left its two
normal model calls and the compact RepairDossier call outside the explicit Stage
Contract/cache authority. This module binds stage identity at the Python call boundary;
prompt wording never selects Draft, Review or Repair.
"""

import functools
import json
from contextvars import ContextVar
from typing import Any

import isco_video_agent.orchestrator as orchestrator
import isco_video_agent.planner as native_short
import isco_video_agent.resilient_planner as staged

from scripts import planning_stage_contract as stage_contract
from scripts.short_planning_repair import active_short_repair_context


_INSTALLED = False
_PILLARS = ("understand", "rise", "see")
_CALL_STATE: ContextVar[dict[str, Any] | None] = ContextVar(
    "isco_native_short_stage_call_state", default=None
)


def _strict_object(properties: dict, required: list[str]) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _string_array(*, exact: int | None = None) -> dict:
    schema: dict = {"type": "array", "items": {"type": "string"}}
    if exact is not None:
        schema["minItems"] = exact
        schema["maxItems"] = exact
    return schema


def _moment_schema() -> dict:
    section = _strict_object(
        {
            "id": {"type": "string"},
            "narration": {"type": "string"},
            "visual_query": {"type": "string"},
            "on_screen_text": {"type": "string"},
            "emotion": {"type": "string"},
            "expected_seconds": {"type": "number"},
            "key_point": {"type": "string"},
        },
        [
            "id",
            "narration",
            "visual_query",
            "on_screen_text",
            "emotion",
            "expected_seconds",
            "key_point",
        ],
    )
    return _strict_object(
        {
            "topic": {"type": "string"},
            # Run168: this is a provider-facing semantic enum, not merely a
            # downstream validator rule. Groq/OpenRouter strict structured output can
            # therefore constrain decoding instead of spending a valid wire attempt on
            # a value the Stage Contract will reject after the response arrives.
            "pillar": {"type": "string", "enum": list(_PILLARS)},
            "format": {"type": "string"},
            "hook": {"type": "string"},
            "title_options": _string_array(exact=3),
            "thumbnail_concepts": _string_array(exact=3),
            "sections": {"type": "array", "items": section, "minItems": 1, "maxItems": 1},
            "cta": {"type": "string"},
            "closing_payoff": {"type": "string"},
        },
        [
            "topic",
            "pillar",
            "format",
            "hook",
            "title_options",
            "thumbnail_concepts",
            "sections",
            "cta",
            "closing_payoff",
        ],
    )


def moment_stage_spec(stage_kind: str, topic: str) -> stage_contract.PlanningStageSpec:
    if stage_kind not in {"short_draft", "short_review", "short_repair"}:
        raise stage_contract.PlanningStageError(
            stage_contract.PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
            f"unsupported native Short stage kind={stage_kind}",
        )
    normalized_topic = " ".join(str(topic or "").strip().split())
    if not normalized_topic:
        raise stage_contract.PlanningStageError(
            stage_contract.PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
            "native Short stage requires approved topic",
            stage_id=f"planning.{stage_kind}",
        )
    return stage_contract.PlanningStageSpec(
        stage_id=f"planning.{stage_kind}",
        contract_id=f"planning.{stage_kind}.v1",
        output_schema=_moment_schema(),
        semantic_rules={
            "kind": "native_short",
            "transport_profile": "native_short",
            "approved_topic": normalized_topic,
            "allowed_pillars": list(_PILLARS),
            "format": "moment",
            "section_count": 1,
            "narration_must_be_empty": True,
            "expected_seconds_min": 7.0,
            "expected_seconds_max": 20.0,
        },
        # Keep the existing conservative generic JSON completion reserve used by the
        # compatibility Moment path. We are adding identity/validation/durability, not
        # inventing a tighter provider-capacity assumption.
        provider_policy=stage_contract._provider_policy(2200),
        cache_policy=stage_contract.CachePolicy(),
    )


def _validate_short_semantics(
    contract: stage_contract.PlanningStageContract,
    data: dict,
) -> None:
    topic = " ".join(str(data.get("topic") or "").strip().split())
    expected_topic = str(contract.semantic_rules.get("approved_topic") or "")
    if topic != expected_topic:
        stage_contract._raise_validation(
            stage_contract.PlanningErrorCode.SEMANTIC_INVALID,
            contract,
            "$.topic",
            "approved_topic_mismatch",
        )
    if str(data.get("format") or "").strip().lower() != "moment":
        stage_contract._raise_validation(
            stage_contract.PlanningErrorCode.SEMANTIC_INVALID,
            contract,
            "$.format",
            "requires_moment",
        )
    if str(data.get("pillar") or "").strip() not in set(_PILLARS):
        stage_contract._raise_validation(
            stage_contract.PlanningErrorCode.SEMANTIC_INVALID,
            contract,
            "$.pillar",
            "unsupported_pillar",
        )
    sections = data.get("sections")
    if not isinstance(sections, list) or len(sections) != 1:
        stage_contract._raise_validation(
            stage_contract.PlanningErrorCode.SEMANTIC_INVALID,
            contract,
            "$.sections",
            "requires_exactly_one_section",
        )
    section = sections[0]
    if str(section.get("narration") or "").strip():
        stage_contract._raise_validation(
            stage_contract.PlanningErrorCode.SEMANTIC_INVALID,
            contract,
            "$.sections[0].narration",
            "moment_narration_must_be_empty",
        )
    for key in ("id", "visual_query", "on_screen_text", "emotion", "key_point"):
        if not str(section.get(key) or "").strip():
            stage_contract._raise_validation(
                stage_contract.PlanningErrorCode.SEMANTIC_INVALID,
                contract,
                f"$.sections[0].{key}",
                "empty",
            )
    seconds = section.get("expected_seconds")
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 7.0 <= float(seconds) <= 20.0:
        stage_contract._raise_validation(
            stage_contract.PlanningErrorCode.SEMANTIC_INVALID,
            contract,
            "$.sections[0].expected_seconds",
            "outside_7_20_seconds",
        )


def _install_stage_contract_extensions() -> None:
    """Extend only the generic Stage Contract's schema/semantic dispatch surfaces."""
    current_schema_tuple = stage_contract._schema_tuple
    if not getattr(current_schema_tuple, "_isco_native_short_stage_v1", False):
        @functools.wraps(current_schema_tuple)
        def schema_tuple(owner):
            profile = str(owner.semantic_rules.get("transport_profile") or "").strip()
            if profile == "native_short":
                return "native_short", owner.output_schema
            return current_schema_tuple(owner)

        schema_tuple._isco_native_short_stage_v1 = True
        schema_tuple._isco_native_short_stage_original = current_schema_tuple
        stage_contract._schema_tuple = schema_tuple

    current_validate = stage_contract.validate_response
    if not getattr(current_validate, "_isco_native_short_stage_v1", False):
        @functools.wraps(current_validate)
        def validate_response(contract, data):
            if str(contract.semantic_rules.get("kind") or "") != "native_short":
                return current_validate(contract, data)
            stage_contract._validate_schema(data, contract.output_schema, contract)
            _validate_short_semantics(contract, data)
            return data

        validate_response._isco_native_short_stage_v1 = True
        validate_response._isco_native_short_stage_original = current_validate
        stage_contract.validate_response = validate_response


def _stage_for_call(state: dict[str, Any]) -> stage_contract.PlanningStageSpec:
    repair = active_short_repair_context()
    if repair is not None:
        current_plan, _issue_notes = repair
        state["calls"] += 1
        return moment_stage_spec("short_repair", getattr(current_plan, "topic", state["topic"]))

    state["calls"] += 1
    if state["calls"] == 1:
        return moment_stage_spec("short_draft", state["topic"])
    if state["calls"] == 2:
        return moment_stage_spec("short_review", state["topic"])
    raise stage_contract.PlanningStageError(
        stage_contract.PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
        f"unexpected native Short provider call index={state['calls']}",
        stage_id="planning.short_unexpected_call",
    )


def _provider_contract_prompt(prompt: str, spec: stage_contract.PlanningStageSpec) -> str:
    """Expose Stage-owned semantic invariants to every provider before generation.

    Run168 proved that a downstream-only enum can reject an otherwise successful Groq
    response even though the model was never told the legal values. The Stage Contract
    remains authoritative; this block simply mirrors its machine-owned invariants into
    the provider request. It is derived from the explicit Python-bound spec, never from
    prompt wording, so it cannot change stage identity.
    """
    topic = str(spec.semantic_rules.get("approved_topic") or "")
    allowed = tuple(str(item) for item in spec.semantic_rules.get("allowed_pillars") or _PILLARS)
    return (
        str(prompt)
        + "\n\nNATIVE_SHORT_STAGE_CONTRACT (mandatory; generated from the active Stage Contract):\n"
        + f"- top-level topic MUST exactly equal {json.dumps(topic, ensure_ascii=False)}.\n"
        + f"- top-level pillar MUST be exactly one of: {' | '.join(allowed)}. "
          "Use these literal English enum values; do not translate or invent another pillar.\n"
        + "- top-level format MUST be exactly: moment.\n"
        + "- sections MUST contain exactly one item.\n"
        + "- sections[0].narration MUST be the empty string.\n"
        + "- sections[0].expected_seconds MUST be between 7 and 20 inclusive.\n"
        + "These constraints do not select the stage; the Python call boundary already owns stage identity."
    )


def install_native_short_stage_contract() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    _install_stage_contract_extensions()

    # Always resolve the explicit router dynamically. Capturing resilient.json_text
    # before install_planning_contract_router() is the historical compatibility bug
    # this contract closes.
    def contracted_json_text(api_key, prompt, model="gemini-2.5-flash"):
        state = _CALL_STATE.get()
        if state is None:
            # Outside the standalone-Moment build lifecycle retain compatibility for
            # unrelated Engine callers rather than inventing a stage.
            return staged.json_text(api_key, prompt, model=model)
        spec = _stage_for_call(state)
        provider_prompt = _provider_contract_prompt(prompt, spec)
        with stage_contract.request_stage_scope(spec):
            return staged.json_text(api_key, provider_prompt, model=model)

    contracted_json_text._isco_native_short_stage_v1 = True
    native_short.json_text = contracted_json_text

    current_build = orchestrator.build_plan
    if not getattr(current_build, "_isco_native_short_stage_parent_v1", False):
        @functools.wraps(current_build)
        def stage_bound_build(*args, **kwargs):
            topic = kwargs.get("topic", args[1] if len(args) > 1 else "")
            requested_format = kwargs.get("requested_format", args[2] if len(args) > 2 else "")
            if str(requested_format or "").strip().lower() != "moment":
                return current_build(*args, **kwargs)

            repair_active = active_short_repair_context() is not None
            state = {"topic": " ".join(str(topic or "").strip().split()), "calls": 0}
            token = _CALL_STATE.set(state)
            try:
                result = current_build(*args, **kwargs)
            except Exception:
                # Never replace the original provider/schema/semantic failure with a
                # secondary call-count assertion.
                raise
            else:
                expected = 1 if repair_active else 2
                if state["calls"] != expected:
                    raise stage_contract.PlanningStageError(
                        stage_contract.PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
                        f"native Short completed with calls={state['calls']} expected={expected}",
                        stage_id="planning.short_lifecycle",
                    )
                return result
            finally:
                _CALL_STATE.reset(token)

        stage_bound_build._isco_native_short_stage_parent_v1 = True
        stage_bound_build._isco_native_short_stage_original = current_build
        orchestrator.build_plan = stage_bound_build

    _INSTALLED = True
    print(
        "Native Short Stage Contract installed: draft=explicit review=explicit "
        "repair=explicit prompt_inference=false cache_revalidate=true provider_semantics=visible"
    )

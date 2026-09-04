from __future__ import annotations

"""Explicit Stage Contract for standalone Moment Draft/Review/Repair calls.

The Engine Moment planner intentionally bypasses resilient_planner's long-form stage
functions, but it still uses the same provider mesh. Stage identity is published by the
actual Engine Draft/Review call sites and consumed here at the provider boundary. Repair
identity remains owned by the explicit repair capability context. Prompt wording and
provider-call ordinal never select Draft, Review or Repair.
"""

import functools
from contextvars import ContextVar
from typing import Any

import isco_video_agent.orchestrator as orchestrator
import isco_video_agent.planner as native_short
import isco_video_agent.resilient_planner as staged
from isco_video_agent.planning_operation import active_planning_operation

from scripts import planning_stage_contract as stage_contract
from scripts.short_planning_repair import active_short_repair_context


_INSTALLED = False
_LIFECYCLE_STATE: ContextVar[dict[str, Any] | None] = ContextVar(
    "isco_native_short_stage_lifecycle_state", default=None
)
_MOMENT_PRODUCTION_MIN_SECONDS = 12.0
_MOMENT_PRODUCTION_MAX_SECONDS = 20.0
_LEGACY_MOMENT_DURATION_GUIDANCE = "moment 7-20 seconds"
_PRODUCTION_MOMENT_DURATION_GUIDANCE = "moment 12-20 seconds"
_MOMENT_DURATION_PROVIDER_CONTRACT = (
    "PRODUCTION MOMENT DURATION CONTRACT: sections[0].expected_seconds MUST be "
    "between 12 and 20 seconds inclusive. Never output a value below 12 seconds."
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
            "pillar": {"type": "string"},
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


def _provider_visible_moment_duration_contract(prompt: object) -> str:
    """Align every standalone-Moment provider call with the downstream quality floor.

    The pinned Engine still contains the historical 7-20s authoring guidance while the
    professional Short quality contract rejects anything below 12s. Rewrite only that
    known duration phrase, then append one explicit contract line so review/repair calls
    that do not repeat the original guidance remain bound to the same 12-20s interval.
    """
    text = str(prompt)
    text = text.replace(
        _LEGACY_MOMENT_DURATION_GUIDANCE,
        _PRODUCTION_MOMENT_DURATION_GUIDANCE,
    )
    if _MOMENT_DURATION_PROVIDER_CONTRACT not in text:
        text = f"{text.rstrip()}\n\n{_MOMENT_DURATION_PROVIDER_CONTRACT}"
    return text


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
            "format": "moment",
            "section_count": 1,
            "narration_must_be_empty": True,
            "expected_seconds_min": _MOMENT_PRODUCTION_MIN_SECONDS,
            "expected_seconds_max": _MOMENT_PRODUCTION_MAX_SECONDS,
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
    if str(data.get("pillar") or "").strip() not in {"understand", "rise", "see"}:
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
    if (
        isinstance(seconds, bool)
        or not isinstance(seconds, (int, float))
        or not _MOMENT_PRODUCTION_MIN_SECONDS
        <= float(seconds)
        <= _MOMENT_PRODUCTION_MAX_SECONDS
    ):
        stage_contract._raise_validation(
            stage_contract.PlanningErrorCode.SEMANTIC_INVALID,
            contract,
            "$.sections[0].expected_seconds",
            "outside_12_20_seconds",
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


def _stage_for_operation(state: dict[str, Any]) -> stage_contract.PlanningStageSpec:
    repair = active_short_repair_context()
    if repair is not None:
        current_plan, _issue_notes = repair
        operation = "short_repair"
        topic = getattr(current_plan, "topic", state["topic"])
        expected = ["short_repair"]
    else:
        operation = active_planning_operation()
        topic = state["topic"]
        expected = ["short_draft", "short_review"]

    if operation not in {"short_draft", "short_review", "short_repair"}:
        raise stage_contract.PlanningStageError(
            stage_contract.PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
            f"native Short provider call lacks explicit named operation={operation!r}",
            stage_id="planning.short_missing_operation",
        )

    observed = state["operations"]
    next_index = len(observed)
    expected_operation = expected[next_index] if next_index < len(expected) else None
    if operation != expected_operation:
        raise stage_contract.PlanningStageError(
            stage_contract.PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
            f"native Short operation sequence expected={expected_operation!r} observed={operation!r}",
            stage_id="planning.short_operation_sequence",
        )
    observed.append(operation)
    return moment_stage_spec(operation, topic)


def install_native_short_stage_contract() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    _install_stage_contract_extensions()

    # Always resolve the explicit router dynamically. Capturing resilient.json_text
    # before install_planning_contract_router() is the historical compatibility bug
    # this contract closes.
    def contracted_json_text(api_key, prompt, model="gemini-2.5-flash"):
        state = _LIFECYCLE_STATE.get()
        if state is None:
            # Outside the standalone-Moment build lifecycle retain compatibility for
            # unrelated Engine callers rather than inventing a stage. Producer repair
            # is explicitly scoped at its own capability boundary.
            return staged.json_text(api_key, prompt, model=model)
        spec = _stage_for_operation(state)
        effective_prompt = _provider_visible_moment_duration_contract(prompt)
        with stage_contract.request_stage_scope(spec):
            return staged.json_text(api_key, effective_prompt, model=model)

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
            state = {
                "topic": " ".join(str(topic or "").strip().split()),
                "operations": [],
            }
            token = _LIFECYCLE_STATE.set(state)
            try:
                result = current_build(*args, **kwargs)
            except Exception:
                # Never replace the original provider/schema/semantic failure with a
                # secondary lifecycle assertion.
                raise
            else:
                expected = ["short_repair"] if repair_active else ["short_draft", "short_review"]
                if state["operations"] != expected:
                    raise stage_contract.PlanningStageError(
                        stage_contract.PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
                        f"native Short completed with operations={state['operations']} expected={expected}",
                        stage_id="planning.short_lifecycle",
                    )
                return result
            finally:
                _LIFECYCLE_STATE.reset(token)

        stage_bound_build._isco_native_short_stage_parent_v1 = True
        stage_bound_build._isco_native_short_stage_original = current_build
        orchestrator.build_plan = stage_bound_build

    _INSTALLED = True
    print(
        "Native Short Stage Contract installed: draft=explicit review=explicit "
        "repair=explicit operation_source=engine_context prompt_inference=false "
        "ordinal_inference=false cache_revalidate=true"
    )

from __future__ import annotations

"""Bind the pinned Engine's split outline topology to exact Runner Stage Contracts.

The pinned Engine already performs two model calls for a long-form outline:
  1) editorial premise / identity (everything except section_briefs),
  2) section_briefs only, using the locked result from call 1.

The Runner historically wrapped the *parent* `_outline()` with one full combined
`editorial_outline` contract. Because the two nested json_text calls inherited that
parent contract, both providers were still forced to emit the obsolete combined JSON
shape. This module closes that Engine<->Runner contract mismatch without changing the
Engine, provider order, quality gates, or final outline shape.

Every subcall receives its own explicit strict schema. The Engine assembles both dicts
locally exactly as before, then the original full outline contract is run once more on
the assembled result. Any Engine call-topology drift (not exactly two model calls) is
fail-closed before the result can become cache/plan authority.
"""

import copy
import functools
from contextvars import ContextVar
from dataclasses import dataclass

import isco_video_agent.resilient_planner as staged

from scripts import planning_stage_contract as stage_contract
from scripts import provider_capacity_hardening as capacity
from scripts import run125_capacity_routing_closure as run125
from scripts import task_level_planner_router as router


CORE_PROFILE = "editorial_outline_core"
SECTIONS_PROFILE = "editorial_outline_sections"
SPLIT_PROFILES = frozenset({CORE_PROFILE, SECTIONS_PROFILE})
_SPLIT_MARKER = "_isco_planning_outline_split_contract_v1"
_SPLIT_JSON_MARKER = "_isco_planning_outline_split_json_v1"
_COMPLETION_TOKENS = stage_contract.OUTLINE_COMPLETION_TOKEN_BUDGET
_INSTALLED = False


@dataclass
class _OutlineCallState:
    fmt: str
    expected_count: int
    call_index: int = 0


_ACTIVE_OUTLINE_CALLS: ContextVar[_OutlineCallState | None] = ContextVar(
    "isco_outline_split_calls", default=None
)
# Run-local only. No prompt/response text is stored. A provider that already proved the
# same exact request fingerprint cannot complete its structured output is not contacted
# again on the optional outer second sweep.
_TERMINAL_REQUEST_FINGERPRINTS: set[tuple[str, str, str]] = set()


def _full_schema(expected_count: int) -> dict:
    return copy.deepcopy(stage_contract._outline_schema(expected_count))


def outline_core_schema(expected_count: int) -> dict:
    schema = _full_schema(expected_count)
    properties = dict(schema["properties"])
    properties.pop("section_briefs", None)
    required = [key for key in schema["required"] if key != "section_briefs"]
    return stage_contract._strict_object(properties, required)


def outline_sections_schema(expected_count: int) -> dict:
    full = _full_schema(expected_count)
    section_briefs = copy.deepcopy(full["properties"]["section_briefs"])
    return stage_contract._strict_object(
        {"section_briefs": section_briefs},
        ["section_briefs"],
    )


def _split_provider_policy() -> stage_contract.ProviderPolicy:
    # Preserve the established P0 provider family and outer attempt budget. The new
    # fingerprint guard below prevents deterministic output-shape failures from making
    # a second HTTP request with the exact same provider/request fingerprint.
    return stage_contract._provider_policy(
        _COMPLETION_TOKENS,
        max_attempts_per_provider=stage_contract.OUTLINE_MAX_ATTEMPTS_PER_PROVIDER,
        max_total_attempts=stage_contract.OUTLINE_MAX_TOTAL_ATTEMPTS,
        second_pass_after_full_exhaustion=True,
    )


def outline_core_stage_spec(expected_count: int) -> stage_contract.PlanningStageSpec:
    if expected_count <= 0:
        raise stage_contract.PlanningStageError(
            stage_contract.PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
            f"invalid expected_count={expected_count}",
            stage_id="planning.editorial_outline_core",
        )
    return stage_contract.PlanningStageSpec(
        stage_id="planning.editorial_outline_core",
        contract_id="planning.editorial_outline_core.v1",
        output_schema=outline_core_schema(expected_count),
        semantic_rules={
            "kind": CORE_PROFILE,
            "transport_profile": CORE_PROFILE,
            "expected_count": expected_count,
            "narrative_identity_gates": True,
        },
        provider_policy=_split_provider_policy(),
        cache_policy=stage_contract.CachePolicy(),
    )


def outline_sections_stage_spec(expected_count: int) -> stage_contract.PlanningStageSpec:
    if expected_count <= 0:
        raise stage_contract.PlanningStageError(
            stage_contract.PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
            f"invalid expected_count={expected_count}",
            stage_id="planning.editorial_outline_sections",
        )
    return stage_contract.PlanningStageSpec(
        stage_id="planning.editorial_outline_sections",
        contract_id="planning.editorial_outline_sections.v1",
        output_schema=outline_sections_schema(expected_count),
        semantic_rules={
            "kind": SECTIONS_PROFILE,
            "transport_profile": SECTIONS_PROFILE,
            "expected_count": expected_count,
            "unique_nonempty_ids": True,
            "nonempty_purpose": True,
        },
        provider_policy=_split_provider_policy(),
        cache_policy=stage_contract.CachePolicy(),
    )


def outline_core_stage_spec_for_format(fmt: str) -> stage_contract.PlanningStageSpec:
    expected = staged._SECTION_COUNTS.get(str(fmt or "").strip().lower())
    if not isinstance(expected, int):
        raise stage_contract.PlanningStageError(
            stage_contract.PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
            f"outline core unknown fmt={fmt}",
            stage_id="planning.editorial_outline_core",
        )
    return outline_core_stage_spec(expected)


def outline_sections_stage_spec_for_format(fmt: str) -> stage_contract.PlanningStageSpec:
    expected = staged._SECTION_COUNTS.get(str(fmt or "").strip().lower())
    if not isinstance(expected, int):
        raise stage_contract.PlanningStageError(
            stage_contract.PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
            f"outline sections unknown fmt={fmt}",
            stage_id="planning.editorial_outline_sections",
        )
    return outline_sections_stage_spec(expected)


def _validate_core(data: dict, contract: stage_contract.PlanningStageContract) -> dict:
    stage_contract._validate_schema(data, contract.output_schema, contract)
    # Reuse the canonical full-outline semantic owner instead of cloning its narrative,
    # identity, and EditorialIntent rules here. Its only brief-specific checks are
    # unique non-empty id/purpose, so one local synthetic brief satisfies that seam.
    # The synthetic value never leaves validation and can never enter cache/plan output.
    semantic_view = dict(data)
    semantic_view["section_briefs"] = [
        {
            "id": "__runner_split_contract_probe__",
            "purpose": "local semantic validation probe",
            "visual_query": "neutral environment",
            "on_screen_text": "probe",
            "emotion": "neutral",
            "expected_seconds": 1,
        }
    ]
    stage_contract._validate_outline_semantics(semantic_view, contract)
    return data


def _validate_sections(data: dict, contract: stage_contract.PlanningStageContract) -> dict:
    stage_contract._validate_schema(data, contract.output_schema, contract)
    briefs = data.get("section_briefs")
    if not isinstance(briefs, list):
        stage_contract._raise_validation(
            stage_contract.PlanningErrorCode.SEMANTIC_INVALID,
            contract,
            "$.section_briefs",
            "expected_array",
        )
    stage_contract._unique_ids(briefs, contract, "section_briefs")
    if any(not str(item.get("purpose") or "").strip() for item in briefs):
        stage_contract._raise_validation(
            stage_contract.PlanningErrorCode.SEMANTIC_INVALID,
            contract,
            "$.section_briefs",
            "empty_purpose",
        )
    return data


def _active_profile() -> str:
    owner = stage_contract._ACTIVE_REQUEST_CONTRACT.get()
    if owner is None:
        owner = stage_contract._ACTIVE_STAGE_SPEC.get()
    if owner is None:
        return ""
    return str(owner.semantic_rules.get("transport_profile") or "").strip()


def _active_fingerprint(provider: str) -> tuple[str, str, str] | None:
    owner = stage_contract._ACTIVE_REQUEST_CONTRACT.get()
    if owner is None or _active_profile() not in SPLIT_PROFILES:
        return None
    return (str(provider), str(owner.contract_id), str(owner.input_hash))


def _groq_schema_generation_failed(error: BaseException | str) -> bool:
    lower = str(error).lower()
    return (
        "groq_json_validate_failed" in lower
        or "code=json_validate_failed" in lower
        or "structured_generation_failed" in lower
        or "failed to validate json" in lower
    )


def _install_schema_and_validation_adapters() -> None:
    if getattr(stage_contract, "_ISCO_OUTLINE_SPLIT_SCHEMA_ADAPTER", False):
        return

    original_schema_tuple = stage_contract._schema_tuple

    def schema_tuple(owner):
        profile = str(owner.semantic_rules.get("transport_profile") or "").strip()
        if profile in SPLIT_PROFILES:
            return profile, owner.output_schema
        return original_schema_tuple(owner)

    original_validate = stage_contract.validate_response

    def validate(contract, data):
        kind = str(contract.semantic_rules.get("kind") or "").strip()
        if kind == CORE_PROFILE:
            return _validate_core(data, contract)
        if kind == SECTIONS_PROFILE:
            return _validate_sections(data, contract)
        return original_validate(contract, data)

    stage_contract._schema_tuple = schema_tuple
    stage_contract.validate_response = validate
    # The lower provider layer owns transport reserve lookup by schema name. Keep the
    # same already-certified 2400-token reserve for both smaller requests.
    capacity._COMPLETION_TOKEN_BUDGETS[CORE_PROFILE] = _COMPLETION_TOKENS
    capacity._COMPLETION_TOKEN_BUDGETS[SECTIONS_PROFILE] = _COMPLETION_TOKENS
    stage_contract._ISCO_OUTLINE_SPLIT_SCHEMA_ADAPTER = True


def _install_groq_model_diversity() -> None:
    if getattr(run125, "_ISCO_OUTLINE_SPLIT_SCHEMA_MODEL_FAILOVER", False):
        return
    original = run125._is_model_unavailable

    def model_unavailable(error) -> bool:
        if original(error):
            return True
        return _active_profile() in SPLIT_PROFILES and _groq_schema_generation_failed(error)

    run125._is_model_unavailable = model_unavailable
    run125._ISCO_OUTLINE_SPLIT_SCHEMA_MODEL_FAILOVER = True


def _install_same_fingerprint_guard() -> None:
    if getattr(router, "_ISCO_OUTLINE_SPLIT_FINGERPRINT_GUARD", False):
        return

    original_gemini = router.gemini_json_text

    @functools.wraps(original_gemini)
    def guarded_gemini(*args, **kwargs):
        fingerprint = _active_fingerprint("gemini")
        if fingerprint is not None and fingerprint in _TERMINAL_REQUEST_FINGERPRINTS:
            raise stage_contract.PlanningStageError(
                stage_contract.PlanningErrorCode.CAPACITY,
                "same_fingerprint_blocked_after_output_truncation",
                stage_id=stage_contract._ACTIVE_REQUEST_CONTRACT.get().stage_id,
                provider="gemini",
            )
        try:
            return original_gemini(*args, **kwargs)
        except Exception as exc:
            if fingerprint is not None and "gemini_interaction_output_truncated" in str(exc).lower():
                _TERMINAL_REQUEST_FINGERPRINTS.add(fingerprint)
            raise

    original_groq = router._groq_call

    @functools.wraps(original_groq)
    def guarded_groq(prompt: str):
        fingerprint = _active_fingerprint("groq")
        if fingerprint is not None and fingerprint in _TERMINAL_REQUEST_FINGERPRINTS:
            raise stage_contract.PlanningStageError(
                stage_contract.PlanningErrorCode.CAPACITY,
                "same_fingerprint_blocked_after_structured_generation_failure",
                stage_id=stage_contract._ACTIVE_REQUEST_CONTRACT.get().stage_id,
                provider="groq",
            )
        try:
            return original_groq(prompt)
        except Exception as exc:
            # This sits *outside* Run125's model pool. The fingerprint is terminal only
            # after its already-bounded 20b -> 120b model-diverse attempt is exhausted.
            if fingerprint is not None and _groq_schema_generation_failed(exc):
                _TERMINAL_REQUEST_FINGERPRINTS.add(fingerprint)
            raise

    router.gemini_json_text = guarded_gemini
    router._groq_call = guarded_groq
    router._ISCO_OUTLINE_SPLIT_FINGERPRINT_GUARD = True


def _certify_engine_topology() -> None:
    for name in ("build_outline_structure_prompt", "build_outline_sections_prompt"):
        if not callable(getattr(staged, name, None)):
            raise stage_contract.PlanningStageError(
                stage_contract.PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
                f"pinned Engine missing split outline port:{name}",
                stage_id="planning.editorial_outline",
            )


def _install_call_sequence_binding() -> None:
    current_json = staged.json_text
    if not getattr(current_json, _SPLIT_JSON_MARKER, False):

        @functools.wraps(current_json)
        def split_json_text(api_key, prompt, model="gemini-2.5-flash"):
            state = _ACTIVE_OUTLINE_CALLS.get()
            if state is None:
                return current_json(api_key, prompt, model=model)

            index = state.call_index
            if index == 0:
                spec = outline_core_stage_spec(state.expected_count)
            elif index == 1:
                spec = outline_sections_stage_spec(state.expected_count)
            else:
                raise stage_contract.PlanningStageError(
                    stage_contract.PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
                    f"pinned Engine outline call topology drifted: unexpected json_text call index={index + 1}",
                    stage_id="planning.editorial_outline",
                )
            # Increment once per Engine-level json_text call. Provider retries happen
            # below current_json and therefore cannot accidentally advance this state.
            state.call_index += 1
            with stage_contract.request_stage_scope(spec):
                return current_json(api_key, prompt, model=model)

        setattr(split_json_text, _SPLIT_JSON_MARKER, True)
        staged.json_text = split_json_text

    current_outline = staged._outline
    if getattr(current_outline, _SPLIT_MARKER, False):
        return

    @functools.wraps(current_outline)
    def split_outline(*args, **kwargs):
        fmt = str(kwargs.get("fmt") or "").strip().lower()
        expected = staged._SECTION_COUNTS.get(fmt)
        if not isinstance(expected, int):
            raise stage_contract.PlanningStageError(
                stage_contract.PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
                f"split outline could not resolve format={fmt or 'missing'}",
                stage_id="planning.editorial_outline",
            )
        state = _OutlineCallState(fmt=fmt, expected_count=expected)
        token = _ACTIVE_OUTLINE_CALLS.set(state)
        try:
            result = current_outline(*args, **kwargs)
        finally:
            _ACTIVE_OUTLINE_CALLS.reset(token)
        if state.call_index != 2:
            raise stage_contract.PlanningStageError(
                stage_contract.PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
                f"pinned Engine outline call topology drifted: expected=2 actual={state.call_index}",
                stage_id="planning.editorial_outline",
            )
        if not isinstance(result, dict):
            raise stage_contract.PlanningStageError(
                stage_contract.PlanningErrorCode.STRUCTURAL_INVALID,
                "assembled outline is not an object",
                stage_id="planning.editorial_outline",
            )

        # Final authority remains the exact historical full outline schema + semantic
        # gates. Splitting changes transport only; acceptance is not weakened.
        full_spec = stage_contract.outline_stage_spec(expected)
        assembled_contract = stage_contract.bind_request_contract(
            full_spec,
            f"local-assembled-outline:{fmt}:{expected}",
        )
        stage_contract.validate_response(assembled_contract, result)
        return result

    setattr(split_outline, _SPLIT_MARKER, True)
    staged._outline = split_outline


def install_planning_outline_split_contract() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _certify_engine_topology()
    _install_schema_and_validation_adapters()
    _install_groq_model_diversity()
    _install_same_fingerprint_guard()
    _install_call_sequence_binding()
    _INSTALLED = True
    print(
        "Planning outline split contract installed: "
        "engine_calls=core->section_briefs exact_subschemas=true "
        "local_full_outline_revalidation=true groq_schema_failure_model_diverse=true "
        "same_fingerprint_terminal_retry=false quality_gates=unchanged"
    )

from __future__ import annotations

"""Bind the pinned Engine's split outline topology to exact Runner Stage Contracts.

The provider-facing outline contracts are transport DTOs. The pinned Engine then
canonicalizes EditorialIntent locally (including host-owned fingerprint/persona
metadata), assembles Core + Section Briefs, and finally exposes a domain object.

This module keeps those layers explicit:

  provider transport -> Engine canonical domain -> canonical JSON projection

That distinction prevents Engine-owned metadata from being rejected by the provider
schema, while keeping `additionalProperties=False` on every model response. It also
fails closed if the pinned Engine dataclasses or split-call topology drift, measures
Call 1b with post-enrichment metadata, and proves plan.json is the exact JSON projection
of the in-memory ProductionPlan before P2/P3 handoff.
"""

import copy
import functools
import json
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import isco_video_agent.resilient_planner as staged

from scripts import planning_stage_contract as stage_contract
from scripts import provider_capacity_hardening as capacity
from scripts import run125_capacity_routing_closure as run125
from scripts import task_level_planner_router as router


CORE_PROFILE = "editorial_outline_core"
SECTIONS_PROFILE = "editorial_outline_sections"
SPLIT_PROFILES = frozenset({CORE_PROFILE, SECTIONS_PROFILE})
_SPLIT_MARKER = "_isco_planning_outline_split_contract_v2"
_SPLIT_JSON_MARKER = "_isco_planning_outline_split_json_v2"
_COMPLETION_TOKENS = stage_contract.OUTLINE_COMPLETION_TOKEN_BUDGET
# Run #209 (and identical Run #204/#205): Gemini truncated real Film/Long editorial_intent
# content at the shared 2400-token budget, which is sized for Groq's own 8000 TPM ceiling
# (confirmed razor-thin for Film by Run #208's GROQ_TPM_WINDOW_BUSY_PRECHECK) and has no
# relationship to Gemini's own, much higher real ceiling. Gemini gets real headroom here;
# Groq's own admission math (planning_envelope_preflight.py) still reads the unchanged
# base _COMPLETION_TOKENS above, so its TPM budget is untouched by this override.
_GEMINI_COMPLETION_TOKENS = _COMPLETION_TOKENS * 2

LOCKED_PREMISE_MAX_UTF8_BYTES = 3_600
LOCKED_PREMISE_BUDGET_MARKER = "<LOCKED_PREMISE_PORTABILITY_BUDGET_V1>"

_PROVIDER_INTENT_FIELDS = (
    "editorial_thesis",
    "viewer_starting_belief",
    "hidden_assumption",
    "editorial_turn",
    "stakes",
    "viewer_promise",
    "evidence_boundaries",
    "earned_payoff",
)
_CANONICAL_INTENT_FIELDS = _PROVIDER_INTENT_FIELDS + (
    "editorial_fingerprint",
    "persona_version",
)
_EXPECTED_PRODUCTION_PLAN_FIELDS = (
    "topic",
    "pillar",
    "format",
    "hook",
    "title_options",
    "thumbnail_concepts",
    "sections",
    "cta",
    "closing_payoff",
    "identity_opener",
    "identity_closer",
    "identity_transitions",
    "narrative_format",
    "editorial_intent",
)
_EXPECTED_SCRIPT_SECTION_FIELDS = (
    "id",
    "narration",
    "visual_query",
    "on_screen_text",
    "emotion",
    "expected_seconds",
    "key_point",
)

_INSTALLED = False


@dataclass
class _OutlineCallState:
    fmt: str
    expected_count: int
    call_index: int = 0


_ACTIVE_OUTLINE_CALLS: ContextVar[_OutlineCallState | None] = ContextVar(
    "isco_outline_split_calls", default=None
)
_TERMINAL_REQUEST_FINGERPRINTS: set[tuple[str, str, str]] = set()


def _json_projection(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


def _full_schema(expected_count: int) -> dict:
    return copy.deepcopy(stage_contract._outline_schema(expected_count))


def provider_editorial_intent_schema() -> dict:
    return copy.deepcopy(_full_schema(1)["properties"]["editorial_intent"])


def canonical_editorial_intent_schema() -> dict:
    schema = provider_editorial_intent_schema()
    properties = dict(schema["properties"])
    properties["editorial_fingerprint"] = {"type": "string"}
    properties["persona_version"] = {"type": "number"}
    return stage_contract._strict_object(properties, list(_CANONICAL_INTENT_FIELDS))


def canonical_outline_schema(expected_count: int) -> dict:
    schema = _full_schema(expected_count)
    properties = dict(schema["properties"])
    properties["editorial_intent"] = canonical_editorial_intent_schema()
    return stage_contract._strict_object(properties, list(schema["required"]))


def outline_core_schema(expected_count: int) -> dict:
    schema = _full_schema(expected_count)
    properties = dict(schema["properties"])
    properties.pop("section_briefs", None)
    required = [key for key in schema["required"] if key != "section_briefs"]
    properties["editorial_intent"] = provider_editorial_intent_schema()
    return stage_contract._strict_object(properties, required)


def outline_sections_schema(expected_count: int) -> dict:
    full = _full_schema(expected_count)
    section_briefs = copy.deepcopy(full["properties"]["section_briefs"])
    return stage_contract._strict_object(
        {"section_briefs": section_briefs},
        ["section_briefs"],
    )


def _split_provider_policy() -> stage_contract.ProviderPolicy:
    return stage_contract._provider_policy(
        _COMPLETION_TOKENS,
        max_attempts_per_provider=stage_contract.OUTLINE_MAX_ATTEMPTS_PER_PROVIDER,
        max_total_attempts=stage_contract.OUTLINE_MAX_TOTAL_ATTEMPTS,
        second_pass_after_full_exhaustion=True,
        completion_tokens_by_provider=(("gemini", _GEMINI_COMPLETION_TOKENS),),
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
        contract_id="planning.editorial_outline_core.transport.v2",
        output_schema=outline_core_schema(expected_count),
        semantic_rules={
            "kind": CORE_PROFILE,
            "transport_profile": CORE_PROFILE,
            "expected_count": expected_count,
            "narrative_identity_gates": True,
            "locked_premise_max_utf8_bytes": LOCKED_PREMISE_MAX_UTF8_BYTES,
            "contract_layer": "provider_transport",
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
        contract_id="planning.editorial_outline_sections.transport.v2",
        output_schema=outline_sections_schema(expected_count),
        semantic_rules={
            "kind": SECTIONS_PROFILE,
            "transport_profile": SECTIONS_PROFILE,
            "expected_count": expected_count,
            "unique_nonempty_ids": True,
            "nonempty_purpose": True,
            "contract_layer": "provider_transport",
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


def _persona_version_for_sizing() -> int:
    import isco_video_agent.editorial_room as editorial_room

    persona = editorial_room.validate_channel_persona(
        editorial_room.load_channel_persona()
    )
    return int(persona["version"])


def _intent_for_transport_sizing(value: object) -> dict:
    projected = _json_projection(value)
    if not isinstance(projected, dict):
        projected = {}
    for key in ("editorial_fingerprint", "persona_version"):
        projected.pop(key, None)
    projected["editorial_fingerprint"] = "0" * 24
    projected["persona_version"] = _persona_version_for_sizing()
    return projected


def locked_premise_for_sizing(data: dict) -> dict:
    result = copy.deepcopy(data)
    result["editorial_intent"] = _intent_for_transport_sizing(
        result.get("editorial_intent")
    )
    return result


def locked_premise_payload(data: dict) -> dict:
    narrative_format = str(data.get("narrative_format") or "").strip()
    return {
        "narrative_format": narrative_format,
        "narrative_format_definition": staged._NARRATIVE_FORMATS.get(
            narrative_format, ""
        ),
        "pillar": str(data.get("pillar") or "").strip(),
        "hook": str(data.get("hook") or "").strip(),
        "closing_payoff": str(data.get("closing_payoff") or "").strip(),
        "editorial_intent": _intent_for_transport_sizing(
            data.get("editorial_intent")
        ),
    }


def locked_premise_utf8_bytes(data: dict) -> int:
    return len(
        json.dumps(
            locked_premise_payload(data),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def core_portability_prompt(prompt: str) -> str:
    """Expose the fail-closed Core -> Sections transport bound to the provider.

    The local byte check remains authoritative. This prompt clause prevents a provider
    from being asked to satisfy a hidden downstream constraint and is deliberately
    Core-only: Section Briefs consume the locked premise but do not author it.
    """
    if LOCKED_PREMISE_BUDGET_MARKER in prompt:
        return prompt
    return (
        prompt
        + "\n\n"
        + LOCKED_PREMISE_BUDGET_MARKER
        + "\n`LOCKED_EDITORIAL_PREMISE` (`narrative_format` plus its host definition, "
        "`pillar`, `hook`, `closing_payoff`, `editorial_intent`) MUST be complete "
        "compact JSON <= "
        + str(LOCKED_PREMISE_MAX_UTF8_BYTES)
        + " UTF-8 bytes; never truncate a sentence or JSON value."
    )


def _validate_core(data: dict, contract: stage_contract.PlanningStageContract) -> dict:
    stage_contract._validate_schema(data, contract.output_schema, contract)
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

    locked_bytes = locked_premise_utf8_bytes(data)
    limit = int(
        contract.semantic_rules.get("locked_premise_max_utf8_bytes")
        or LOCKED_PREMISE_MAX_UTF8_BYTES
    )
    if locked_bytes > limit:
        stage_contract._raise_validation(
            stage_contract.PlanningErrorCode.SEMANTIC_INVALID,
            contract,
            "$",
            f"locked_premise_portability_budget_exceeded bytes={locked_bytes} limit={limit}",
        )
    return data


def _validate_sections(
    data: dict, contract: stage_contract.PlanningStageContract
) -> dict:
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


def _canonical_engine_intent(value: object) -> dict:
    intent = staged.intent_from_dict(_json_projection(value))
    return _json_projection(intent.to_dict())


def _validate_canonical_editorial_intent(
    value: object,
    contract: stage_contract.PlanningStageContract,
) -> dict:
    projected = _json_projection(value)
    stage_contract._validate_schema(
        projected,
        canonical_editorial_intent_schema(),
        contract,
        "$.editorial_intent",
    )
    if (
        isinstance(projected.get("persona_version"), bool)
        or not isinstance(projected.get("persona_version"), int)
    ):
        stage_contract._raise_validation(
            stage_contract.PlanningErrorCode.SEMANTIC_INVALID,
            contract,
            "$.editorial_intent.persona_version",
            "expected_engine_integer_persona_version",
        )
    try:
        recomputed = _canonical_engine_intent(projected)
    except Exception as exc:
        stage_contract._raise_validation(
            stage_contract.PlanningErrorCode.SEMANTIC_INVALID,
            contract,
            "$.editorial_intent",
            f"engine_canonicalization_failed:{type(exc).__name__}",
        )
    if projected.get("editorial_fingerprint") != recomputed.get(
        "editorial_fingerprint"
    ):
        stage_contract._raise_validation(
            stage_contract.PlanningErrorCode.SEMANTIC_INVALID,
            contract,
            "$.editorial_intent.editorial_fingerprint",
            "host_fingerprint_mismatch",
        )
    if projected.get("persona_version") != recomputed.get("persona_version"):
        stage_contract._raise_validation(
            stage_contract.PlanningErrorCode.SEMANTIC_INVALID,
            contract,
            "$.editorial_intent.persona_version",
            "host_persona_version_mismatch",
        )
    if projected != recomputed:
        differing = sorted(
            key
            for key in set(projected) | set(recomputed)
            if projected.get(key) != recomputed.get(key)
        )
        stage_contract._raise_validation(
            stage_contract.PlanningErrorCode.SEMANTIC_INVALID,
            contract,
            "$.editorial_intent",
            "canonical_roundtrip_mismatch=" + ",".join(differing),
        )
    return projected


def _validate_canonical_outline(
    data: dict,
    contract: stage_contract.PlanningStageContract,
    expected_count: int,
) -> dict:
    projected = _json_projection(data)
    stage_contract._validate_schema(
        projected,
        canonical_outline_schema(expected_count),
        contract,
    )
    _validate_canonical_editorial_intent(
        projected.get("editorial_intent"),
        contract,
    )
    stage_contract._validate_outline_semantics(projected, contract)
    return projected


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
    if getattr(stage_contract, "_ISCO_OUTLINE_SPLIT_SCHEMA_ADAPTER_V2", False):
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
    capacity._COMPLETION_TOKEN_BUDGETS[CORE_PROFILE] = _COMPLETION_TOKENS
    capacity._COMPLETION_TOKEN_BUDGETS[SECTIONS_PROFILE] = _COMPLETION_TOKENS
    stage_contract._ISCO_OUTLINE_SPLIT_SCHEMA_ADAPTER_V2 = True


def _install_groq_model_diversity() -> None:
    if getattr(run125, "_ISCO_OUTLINE_SPLIT_SCHEMA_MODEL_FAILOVER_V2", False):
        return
    original = run125._is_model_unavailable

    def model_unavailable(error) -> bool:
        if original(error):
            return True
        return _active_profile() in SPLIT_PROFILES and _groq_schema_generation_failed(
            error
        )

    run125._is_model_unavailable = model_unavailable
    run125._ISCO_OUTLINE_SPLIT_SCHEMA_MODEL_FAILOVER_V2 = True


def _install_same_fingerprint_guard() -> None:
    if getattr(router, "_ISCO_OUTLINE_SPLIT_FINGERPRINT_GUARD_V2", False):
        return

    original_gemini = router.gemini_json_text

    @functools.wraps(original_gemini)
    def guarded_gemini(*args, **kwargs):
        fingerprint = _active_fingerprint("gemini")
        if fingerprint is not None and fingerprint in _TERMINAL_REQUEST_FINGERPRINTS:
            owner = stage_contract._ACTIVE_REQUEST_CONTRACT.get()
            raise stage_contract.PlanningStageError(
                stage_contract.PlanningErrorCode.CAPACITY,
                "same_fingerprint_blocked_after_output_truncation",
                stage_id=getattr(owner, "stage_id", None),
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
            owner = stage_contract._ACTIVE_REQUEST_CONTRACT.get()
            raise stage_contract.PlanningStageError(
                stage_contract.PlanningErrorCode.CAPACITY,
                "same_fingerprint_blocked_after_structured_generation_failure",
                stage_id=getattr(owner, "stage_id", None),
                provider="groq",
            )
        try:
            return original_groq(prompt)
        except Exception as exc:
            if fingerprint is not None and _groq_schema_generation_failed(exc):
                _TERMINAL_REQUEST_FINGERPRINTS.add(fingerprint)
            raise

    router.gemini_json_text = guarded_gemini
    router._groq_call = guarded_groq
    router._ISCO_OUTLINE_SPLIT_FINGERPRINT_GUARD_V2 = True


def _certify_engine_topology() -> None:
    for name in (
        "build_outline_structure_prompt",
        "build_outline_sections_prompt",
        "intent_from_dict",
    ):
        if not callable(getattr(staged, name, None)):
            raise stage_contract.PlanningStageError(
                stage_contract.PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
                f"pinned Engine missing planning contract port:{name}",
                stage_id="planning.editorial_outline",
            )


def assert_engine_runner_contract_compatible() -> dict[str, tuple[str, ...]]:
    from isco_video_agent.editorial_room import EditorialIntent
    from isco_video_agent.models import ProductionPlan, ScriptSection

    observed = {
        "EditorialIntent": tuple(EditorialIntent.__dataclass_fields__),
        "ProductionPlan": tuple(ProductionPlan.__dataclass_fields__),
        "ScriptSection": tuple(ScriptSection.__dataclass_fields__),
    }
    expected = {
        "EditorialIntent": _CANONICAL_INTENT_FIELDS,
        "ProductionPlan": _EXPECTED_PRODUCTION_PLAN_FIELDS,
        "ScriptSection": _EXPECTED_SCRIPT_SECTION_FIELDS,
    }
    drift = {
        name: (expected[name], observed[name])
        for name in expected
        if observed[name] != expected[name]
    }
    if drift:
        details = "; ".join(
            f"{name}:expected={exp} observed={obs}"
            for name, (exp, obs) in drift.items()
        )
        raise stage_contract.PlanningStageError(
            stage_contract.PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
            "ENGINE_RUNNER_DOMAIN_CONTRACT_DRIFT " + details,
            stage_id="planning.engine_runner_contract",
        )

    provider_fields = tuple(provider_editorial_intent_schema()["required"])
    if provider_fields != _PROVIDER_INTENT_FIELDS:
        raise stage_contract.PlanningStageError(
            stage_contract.PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
            f"ENGINE_RUNNER_PROVIDER_DTO_DRIFT expected={_PROVIDER_INTENT_FIELDS} observed={provider_fields}",
            stage_id="planning.engine_runner_contract",
        )
    return observed


def _assert_exact_plan_json_projection(
    output_dir: Path,
    plan_object: object | None,
) -> None:
    if plan_object is None:
        return
    to_dict = getattr(plan_object, "to_dict", None)
    if not callable(to_dict):
        raise stage_contract.PlanningStageError(
            stage_contract.PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
            "final plan object has no canonical to_dict projection",
            stage_id="planning.final_plan_projection",
        )
    path = Path(output_dir) / "plan.json"
    if not path.is_file() or path.is_symlink():
        return
    try:
        actual = json.loads(path.read_text(encoding="utf-8"))
        expected = _json_projection(to_dict())
    except Exception as exc:
        raise stage_contract.PlanningStageError(
            stage_contract.PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
            f"final plan projection unreadable:{type(exc).__name__}",
            stage_id="planning.final_plan_projection",
        ) from exc
    if not isinstance(actual, dict) or not isinstance(expected, dict):
        raise stage_contract.PlanningStageError(
            stage_contract.PlanningErrorCode.STRUCTURAL_INVALID,
            "final plan projection is not an object",
            stage_id="planning.final_plan_projection",
        )

    actual = dict(actual)
    expected = dict(expected)
    actual.pop("plan_source", None)
    expected.pop("plan_source", None)

    if actual != expected:
        actual_keys = set(actual)
        expected_keys = set(expected)
        missing = sorted(expected_keys - actual_keys)
        extra = sorted(actual_keys - expected_keys)
        changed = sorted(
            key
            for key in actual_keys & expected_keys
            if actual.get(key) != expected.get(key)
        )
        raise stage_contract.PlanningStageError(
            stage_contract.PlanningErrorCode.STRUCTURAL_INVALID,
            f"plan_json_domain_projection_mismatch missing={missing} extra={extra} changed={changed}",
            stage_id="planning.final_plan_projection",
        )


def _install_plan_handoff_equivalence_guard() -> None:
    import scripts.planning_production_contract_v2 as production_contract

    current = production_contract.certify_planning_handoff
    if getattr(current, "_isco_exact_plan_projection_v1", False):
        return

    @functools.wraps(current)
    def guarded(output_dir, plan_object=None):
        _assert_exact_plan_json_projection(Path(output_dir), plan_object)
        return current(output_dir, plan_object)

    guarded._isco_exact_plan_projection_v1 = True
    guarded._isco_original = current
    production_contract.certify_planning_handoff = guarded


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
                effective_prompt = core_portability_prompt(prompt)
            elif index == 1:
                spec = outline_sections_stage_spec(state.expected_count)
                effective_prompt = prompt
            else:
                raise stage_contract.PlanningStageError(
                    stage_contract.PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
                    f"pinned Engine outline call topology drifted: unexpected json_text call index={index + 1}",
                    stage_id="planning.editorial_outline",
                )
            state.call_index += 1
            with stage_contract.request_stage_scope(spec):
                return current_json(api_key, effective_prompt, model=model)

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

        full_spec = stage_contract.outline_stage_spec(expected)
        assembled_contract = stage_contract.bind_request_contract(
            full_spec,
            f"local-canonical-assembled-outline:v2:{fmt}:{expected}",
        )
        _validate_canonical_outline(result, assembled_contract, expected)
        return result

    setattr(split_outline, _SPLIT_MARKER, True)
    staged._outline = split_outline


def install_planning_outline_split_contract() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _certify_engine_topology()
    assert_engine_runner_contract_compatible()
    _install_schema_and_validation_adapters()
    _install_groq_model_diversity()
    _install_same_fingerprint_guard()
    _install_call_sequence_binding()
    _install_plan_handoff_equivalence_guard()
    _INSTALLED = True
    print(
        "Planning outline split contract v2 installed: "
        "engine_calls=core->section_briefs provider_transport_schema=strict "
        "engine_canonical_schema=strict host_metadata=fingerprint+persona_version "
        f"locked_premise_post_enrichment_max_utf8_bytes={LOCKED_PREMISE_MAX_UTF8_BYTES} "
        "local_canonical_outline_revalidation=true plan_json_exact_domain_projection=true "
        "engine_runner_domain_drift=fail_closed groq_schema_failure_model_diverse=true "
        "same_fingerprint_terminal_retry=false quality_gates=unchanged"
    )

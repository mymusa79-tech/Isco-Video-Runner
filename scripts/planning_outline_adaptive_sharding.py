from __future__ import annotations

"""Adaptive bounded sharding for the long-form Core -> Sections outline seam.

Fast path is unchanged at two Engine model tasks:

    Core(+Global Skeleton) -> Sections[all]

Only a Runner-certified, non-transient Section transport exhaustion may split the failed
batch.  Film therefore degrades 8 -> 4 -> 2 -> 1 (Story uses the same balanced rule for
its five sections).  Successful siblings are never regenerated.  Every shard receives
the immutable Core-owned Global Skeleton and must copy id/purpose verbatim.

Provider retry remains owned by planning_stage_contract.  This module adds a second,
global hard ceiling over *actual provider-family attempts* across the whole outline so
the recursive topology can never multiply retries without bound.
"""

import copy
import functools
import math
from dataclasses import dataclass, field
from typing import Sequence

import isco_video_agent.resilient_planner as staged
from isco_video_agent import adaptive_outline_contract as engine_contract

from scripts import planning_outline_split_contract as split_contract
from scripts import planning_stage_contract as stage_contract
from scripts import provider_capacity_hardening as capacity


_ADAPTIVE_MARKER = "_isco_planning_outline_adaptive_sharding_v1"
_ADAPTIVE_JSON_MARKER = "_isco_planning_outline_adaptive_json_v1"
_PROVIDER_BUDGET_MARKER = "_isco_planning_outline_provider_budget_v1"
_INSTALLED = False

# Existing Core/full-request policy may use at most six provider-family attempts
# (3 families x at most one bounded second sweep).  A shard gets one sweep only: the
# taxonomy has already certified that splitting, rather than replaying the same request,
# is the recovery operation.  For an 8-section Film, allowing one failed branch to be
# isolated all the way 8 -> 4 -> 2 -> 1 while each sibling gets its full one-sweep mesh
# costs at most: Core 6 + root Sections 6 + (2 children x 3 providers x 3 split levels)
# = 30 real provider-family attempts.  That is the hard global ceiling.  If several
# branches fail deeply, the budget trips fail-closed before a full 54-attempt binary
# tree can ever be explored.
OUTLINE_PROVIDER_ATTEMPT_HARD_MAX = 30
SHARD_MAX_TOTAL_ATTEMPTS = 3

# Root Sections no longer needs the legacy 2400-token all-outline reserve.  It emits
# only compact briefs and now carries a Skeleton in input, so scale output reserve by
# batch size to protect Groq's 8K TPM window.  Core keeps the existing policy unchanged.
def _section_completion_tokens(count: int) -> int:
    return min(1_800, max(600, 400 + (200 * int(count))))


def _sections_profile(count: int) -> str:
    return f"{split_contract.SECTIONS_PROFILE}_{int(count)}"


def _all_split_profiles() -> frozenset[str]:
    largest = max(int(value) for value in staged._SECTION_COUNTS.values())
    return frozenset(
        {split_contract.CORE_PROFILE}
        | {_sections_profile(count) for count in range(1, largest + 1)}
    )


@dataclass
class _AdaptiveOutlineState:
    fmt: str
    expected_count: int
    phase: str = "core_pending"
    skeleton: list[dict] | None = None
    provider_attempts: int = 0
    section_request_count: int = 0
    successful_batches: list[list[dict]] = field(default_factory=list)
    request_trace: list[tuple[str, ...]] = field(default_factory=list)

    @property
    def max_section_requests(self) -> int:
        return engine_contract.max_section_request_nodes(self.expected_count)

    @property
    def provider_attempt_limit(self) -> int:
        # Current supported Long formats are <=8 sections, so the evidence-derived
        # depth budget is never allowed to exceed the explicit global hard maximum.
        depth = max(0, math.ceil(math.log2(self.expected_count)))
        derived = (
            stage_contract.OUTLINE_MAX_TOTAL_ATTEMPTS
            + stage_contract.OUTLINE_MAX_TOTAL_ATTEMPTS
            + (2 * depth * SHARD_MAX_TOTAL_ATTEMPTS)
        )
        return min(OUTLINE_PROVIDER_ATTEMPT_HARD_MAX, derived)


_SPLIT_DENY_MARKERS = (
    "tpm_window",
    "tpm_capacity_preflight",
    "groq_tpm_window_busy_precheck",
    "status=429",
    "http_429",
    "rate limit",
    "quota",
    "spend cap",
    "timeout",
    "timed out",
    "network",
    "connection",
    "temporar",
    "service unavailable",
)
_SPLIT_ALLOW_MARKERS = (
    "gemini_interaction_output_truncated",
    "output_truncated",
    "incomplete_max_tokens",
    "structured_generation_failed",
    "structured_generation_failure",
    "json_validate_failed",
    "failed to validate json",
    "payload_too_large",
    "context_length",
    "actual_tpm_below_request",
    "max_tokens",
)


def is_shardable_sections_failure(error: BaseException) -> bool:
    """Conservative allow-list: any transient/window evidence vetoes sharding."""
    text = str(error).lower()
    if any(marker in text for marker in _SPLIT_DENY_MARKERS):
        return False
    if not any(marker in text for marker in _SPLIT_ALLOW_MARKERS):
        return False
    if isinstance(error, stage_contract.PlanningStageError):
        return error.code in {
            stage_contract.PlanningErrorCode.CAPACITY,
            stage_contract.PlanningErrorCode.STRUCTURAL_INVALID,
        }
    return True


def _skeleton_schema(expected_count: int) -> dict:
    item = stage_contract._strict_object(
        {
            "id": {"type": "string"},
            "purpose": {"type": "string"},
            "arc_position": {"type": "number"},
        },
        ["id", "purpose", "arc_position"],
    )
    return {
        "type": "array",
        "items": item,
        "minItems": expected_count,
        "maxItems": expected_count,
    }


def outline_core_schema(expected_count: int) -> dict:
    schema = split_contract._full_schema(expected_count)
    properties = dict(schema["properties"])
    properties.pop("section_briefs", None)
    properties[engine_contract.GLOBAL_SECTION_SKELETON_FIELD] = _skeleton_schema(
        expected_count
    )
    properties["editorial_intent"] = split_contract.provider_editorial_intent_schema()
    required = [key for key in schema["required"] if key != "section_briefs"]
    required.append(engine_contract.GLOBAL_SECTION_SKELETON_FIELD)
    return stage_contract._strict_object(properties, required)


def outline_sections_schema(expected_count: int) -> dict:
    return split_contract.outline_sections_schema(expected_count)


def _core_stage_spec(expected_count: int) -> stage_contract.PlanningStageSpec:
    return stage_contract.PlanningStageSpec(
        stage_id="planning.editorial_outline_core",
        contract_id="planning.editorial_outline_core.transport.v3",
        output_schema=outline_core_schema(expected_count),
        semantic_rules={
            "kind": split_contract.CORE_PROFILE,
            "transport_profile": split_contract.CORE_PROFILE,
            "expected_count": expected_count,
            "narrative_identity_gates": True,
            "locked_premise_max_utf8_bytes": split_contract.LOCKED_PREMISE_MAX_UTF8_BYTES,
            "global_skeleton_max_utf8_bytes": engine_contract.GLOBAL_SECTION_SKELETON_MAX_UTF8_BYTES,
            "contract_layer": "provider_transport",
        },
        provider_policy=split_contract._split_provider_policy(),
        cache_policy=stage_contract.CachePolicy(),
    )


def _sections_stage_spec(
    skeleton: list[dict],
    requested_ids: Sequence[str],
    *,
    root: bool,
) -> stage_contract.PlanningStageSpec:
    ids = tuple(str(item) for item in requested_ids)
    count = len(ids)
    profile = _sections_profile(count)
    completion_tokens = _section_completion_tokens(count)
    provider_policy = stage_contract._provider_policy(
        completion_tokens,
        max_attempts_per_provider=stage_contract.OUTLINE_MAX_ATTEMPTS_PER_PROVIDER,
        max_total_attempts=(
            stage_contract.OUTLINE_MAX_TOTAL_ATTEMPTS if root else SHARD_MAX_TOTAL_ATTEMPTS
        ),
        second_pass_after_full_exhaustion=root,
        # Preserve the established provider asymmetry from split v2: Groq's reserve is
        # deliberately constrained by its shared 8K TPM window, while Gemini previously
        # proved it needs 2x completion headroom to avoid truncating valid Film output.
        # Scaling both sides by shard size keeps that protection without increasing
        # Groq TPM pressure or changing the #579 rolling-window recovery contract.
        completion_tokens_by_provider=(("gemini", completion_tokens * 2),),
    )
    purpose_by_id = {item["id"]: item["purpose"] for item in skeleton}
    return stage_contract.PlanningStageSpec(
        stage_id="planning.editorial_outline_sections",
        contract_id=(
            "planning.editorial_outline_sections.root.transport.v3"
            if root
            else f"planning.editorial_outline_sections.shard{count}.transport.v3"
        ),
        output_schema=outline_sections_schema(count),
        semantic_rules={
            "kind": split_contract.SECTIONS_PROFILE,
            "transport_profile": profile,
            "expected_count": count,
            "expected_ids": list(ids),
            "expected_purposes": [purpose_by_id[item] for item in ids],
            "global_skeleton": copy.deepcopy(skeleton),
            "root_sections_request": bool(root),
            "contract_layer": "provider_transport",
        },
        provider_policy=provider_policy,
        cache_policy=stage_contract.CachePolicy(),
    )


def _validate_core(
    data: dict,
    contract: stage_contract.PlanningStageContract,
) -> dict:
    stage_contract._validate_schema(data, contract.output_schema, contract)

    # Reuse the established full-outline semantic validator with a local probe exactly
    # as v2 did; Global Skeleton has its own stricter deterministic contract below.
    semantic_view = dict(data)
    semantic_view.pop(engine_contract.GLOBAL_SECTION_SKELETON_FIELD, None)
    semantic_view["section_briefs"] = [
        {
            "id": "__runner_adaptive_contract_probe__",
            "purpose": "local semantic validation probe",
            "visual_query": "neutral environment",
            "on_screen_text": "probe",
            "emotion": "neutral",
            "expected_seconds": 1,
        }
    ]
    stage_contract._validate_outline_semantics(semantic_view, contract)

    locked_bytes = split_contract.locked_premise_utf8_bytes(data)
    locked_limit = int(
        contract.semantic_rules.get("locked_premise_max_utf8_bytes")
        or split_contract.LOCKED_PREMISE_MAX_UTF8_BYTES
    )
    if locked_bytes > locked_limit:
        stage_contract._raise_validation(
            stage_contract.PlanningErrorCode.SEMANTIC_INVALID,
            contract,
            "$",
            f"locked_premise_portability_budget_exceeded bytes={locked_bytes} limit={locked_limit}",
        )
    try:
        skeleton = engine_contract.validate_global_section_skeleton(
            data.get(engine_contract.GLOBAL_SECTION_SKELETON_FIELD),
            int(contract.semantic_rules["expected_count"]),
        )
    except engine_contract.AdaptiveOutlineContractError as exc:
        stage_contract._raise_validation(
            stage_contract.PlanningErrorCode.SEMANTIC_INVALID,
            contract,
            f"$.{engine_contract.GLOBAL_SECTION_SKELETON_FIELD}",
            str(exc)[:300],
        )
    data[engine_contract.GLOBAL_SECTION_SKELETON_FIELD] = skeleton
    return data


def _validate_sections(
    data: dict,
    contract: stage_contract.PlanningStageContract,
) -> dict:
    stage_contract._validate_schema(data, contract.output_schema, contract)
    try:
        engine_contract.validate_expanded_section_briefs(
            data.get("section_briefs"),
            contract.semantic_rules.get("global_skeleton"),
            tuple(contract.semantic_rules.get("expected_ids") or ()),
        )
    except engine_contract.AdaptiveOutlineContractError as exc:
        stage_contract._raise_validation(
            stage_contract.PlanningErrorCode.SEMANTIC_INVALID,
            contract,
            "$.section_briefs",
            str(exc)[:300],
        )
    return data


def _install_provider_attempt_budget_guard() -> None:
    current = stage_contract._provider_result
    if getattr(current, _PROVIDER_BUDGET_MARKER, False):
        return

    @functools.wraps(current)
    def guarded(provider, prompt, model, contract, primary_api_key):
        state = split_contract._ACTIVE_OUTLINE_CALLS.get()
        if isinstance(state, _AdaptiveOutlineState):
            if state.provider_attempts >= state.provider_attempt_limit:
                raise stage_contract.PlanningStageError(
                    stage_contract.PlanningErrorCode.CAPACITY,
                    "outline_provider_attempt_budget_exhausted "
                    f"attempts={state.provider_attempts} limit={state.provider_attempt_limit}",
                    stage_id=getattr(contract, "stage_id", "planning.editorial_outline"),
                    provider=str(provider),
                )
            state.provider_attempts += 1
        return current(provider, prompt, model, contract, primary_api_key)

    setattr(guarded, _PROVIDER_BUDGET_MARKER, True)
    stage_contract._provider_result = guarded


def _request_section_batch(
    current_json,
    *,
    api_key: str,
    model: str,
    base_prompt: str,
    state: _AdaptiveOutlineState,
    requested_ids: tuple[str, ...],
    root: bool,
) -> list[dict]:
    state.section_request_count += 1
    if state.section_request_count > state.max_section_requests:
        raise stage_contract.PlanningStageError(
            stage_contract.PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
            "adaptive outline section tree exceeded deterministic full-tree bound "
            f"actual={state.section_request_count} limit={state.max_section_requests}",
            stage_id="planning.editorial_outline_sections",
        )
    state.request_trace.append(requested_ids)

    effective_prompt = engine_contract.sections_prompt_for_ids(
        base_prompt,
        state.skeleton,
        requested_ids,
    )
    spec = _sections_stage_spec(state.skeleton or [], requested_ids, root=root)
    try:
        with stage_contract.request_stage_scope(spec):
            payload = current_json(api_key, effective_prompt, model=model)
    except Exception as exc:
        if len(requested_ids) <= 1 or not is_shardable_sections_failure(exc):
            raise
        left, right = engine_contract.split_section_ids(requested_ids)
        print(
            "Planning outline adaptive shard: "
            f"failed={list(requested_ids)} -> left={list(left)} right={list(right)} "
            f"provider_attempts={state.provider_attempts}/{state.provider_attempt_limit}"
        )
        left_briefs = _request_section_batch(
            current_json,
            api_key=api_key,
            model=model,
            base_prompt=base_prompt,
            state=state,
            requested_ids=left,
            root=False,
        )
        right_briefs = _request_section_batch(
            current_json,
            api_key=api_key,
            model=model,
            base_prompt=base_prompt,
            state=state,
            requested_ids=right,
            root=False,
        )
        return left_briefs + right_briefs

    briefs = engine_contract.validate_expanded_section_briefs(
        payload.get("section_briefs"),
        state.skeleton,
        requested_ids,
    )
    state.successful_batches.append(briefs)
    return briefs


def _install_adaptive_call_state_machine() -> None:
    current_json = staged.json_text
    if not getattr(current_json, _ADAPTIVE_JSON_MARKER, False):

        @functools.wraps(current_json)
        def adaptive_json_text(api_key, prompt, model="gemini-2.5-flash"):
            state = split_contract._ACTIVE_OUTLINE_CALLS.get()
            if not isinstance(state, _AdaptiveOutlineState):
                return current_json(api_key, prompt, model=model)

            if state.phase == "core_pending":
                state.phase = "core_inflight"
                spec = _core_stage_spec(state.expected_count)
                effective_prompt = engine_contract.core_prompt_with_global_skeleton(
                    split_contract.core_portability_prompt(prompt),
                    state.expected_count,
                )
                try:
                    with stage_contract.request_stage_scope(spec):
                        payload = current_json(api_key, effective_prompt, model=model)
                except Exception:
                    state.phase = "failed"
                    raise
                state.skeleton = engine_contract.validate_global_section_skeleton(
                    payload.get(engine_contract.GLOBAL_SECTION_SKELETON_FIELD),
                    state.expected_count,
                )
                state.phase = "sections_pending"
                return payload

            if state.phase == "sections_pending":
                if state.skeleton is None:
                    raise stage_contract.PlanningStageError(
                        stage_contract.PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
                        "adaptive sections phase has no certified Global Skeleton",
                        stage_id="planning.editorial_outline_sections",
                    )
                state.phase = "sections_inflight"
                all_ids = tuple(item["id"] for item in state.skeleton)
                try:
                    briefs = _request_section_batch(
                        current_json,
                        api_key=api_key,
                        model=model,
                        base_prompt=prompt,
                        state=state,
                        requested_ids=all_ids,
                        root=True,
                    )
                except Exception:
                    state.phase = "failed"
                    raise
                merged = engine_contract.merge_section_brief_batches(
                    state.successful_batches,
                    state.skeleton,
                )
                if merged != briefs:
                    raise stage_contract.PlanningStageError(
                        stage_contract.PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
                        "adaptive section merge diverged from recursive result",
                        stage_id="planning.editorial_outline_sections",
                    )
                state.phase = "complete"
                return {"section_briefs": merged}

            raise stage_contract.PlanningStageError(
                stage_contract.PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
                f"adaptive outline state-machine drift phase={state.phase}",
                stage_id="planning.editorial_outline",
            )

        setattr(adaptive_json_text, _ADAPTIVE_JSON_MARKER, True)
        staged.json_text = adaptive_json_text

    current_outline = staged._outline
    if getattr(current_outline, _ADAPTIVE_MARKER, False):
        return

    @functools.wraps(current_outline)
    def adaptive_outline(*args, **kwargs):
        fmt = str(kwargs.get("fmt") or "").strip().lower()
        expected = staged._SECTION_COUNTS.get(fmt)
        if not isinstance(expected, int):
            raise stage_contract.PlanningStageError(
                stage_contract.PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
                f"adaptive outline could not resolve format={fmt or 'missing'}",
                stage_id="planning.editorial_outline",
            )
        state = _AdaptiveOutlineState(fmt=fmt, expected_count=expected)
        token = split_contract._ACTIVE_OUTLINE_CALLS.set(state)
        try:
            result = current_outline(*args, **kwargs)
        finally:
            split_contract._ACTIVE_OUTLINE_CALLS.reset(token)

        if state.phase != "complete" or state.skeleton is None:
            raise stage_contract.PlanningStageError(
                stage_contract.PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
                f"adaptive outline topology incomplete phase={state.phase}",
                stage_id="planning.editorial_outline",
            )
        if not isinstance(result, dict):
            raise stage_contract.PlanningStageError(
                stage_contract.PlanningErrorCode.STRUCTURAL_INVALID,
                "assembled adaptive outline is not an object",
                stage_id="planning.editorial_outline",
            )

        observed_skeleton = result.pop(
            engine_contract.GLOBAL_SECTION_SKELETON_FIELD,
            None,
        )
        if engine_contract.validate_global_section_skeleton(
            observed_skeleton,
            expected,
        ) != state.skeleton:
            raise stage_contract.PlanningStageError(
                stage_contract.PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
                "Core Global Skeleton changed before canonical outline handoff",
                stage_id="planning.editorial_outline",
            )

        all_ids = tuple(item["id"] for item in state.skeleton)
        final_briefs = engine_contract.validate_expanded_section_briefs(
            result.get("section_briefs"),
            state.skeleton,
            all_ids,
        )
        merged_from_leaves = engine_contract.merge_section_brief_batches(
            state.successful_batches,
            state.skeleton,
        )
        if final_briefs != merged_from_leaves:
            raise stage_contract.PlanningStageError(
                stage_contract.PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
                "final outline sections are not the exact successful-shard merge",
                stage_id="planning.editorial_outline",
            )

        full_spec = stage_contract.outline_stage_spec(expected)
        assembled_contract = stage_contract.bind_request_contract(
            full_spec,
            f"local-canonical-adaptive-outline:v3:{fmt}:{expected}",
        )
        split_contract._validate_canonical_outline(
            result,
            assembled_contract,
            expected,
        )
        print(
            "Planning outline adaptive topology complete: "
            f"format={fmt} section_requests={state.section_request_count}/"
            f"{state.max_section_requests} provider_attempts={state.provider_attempts}/"
            f"{state.provider_attempt_limit} trace={state.request_trace}"
        )
        return result

    setattr(adaptive_outline, _ADAPTIVE_MARKER, True)
    staged._outline = adaptive_outline


def _certify_engine_adaptive_port() -> None:
    for name in (
        "core_prompt_with_global_skeleton",
        "sections_prompt_for_ids",
        "validate_global_section_skeleton",
        "validate_expanded_section_briefs",
        "merge_section_brief_batches",
        "split_section_ids",
        "max_section_request_nodes",
    ):
        if not callable(getattr(engine_contract, name, None)):
            raise stage_contract.PlanningStageError(
                stage_contract.PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
                f"pinned Engine missing adaptive outline contract port:{name}",
                stage_id="planning.engine_runner_contract",
            )


def install_planning_outline_adaptive_sharding() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    split_contract._certify_engine_topology()
    _certify_engine_adaptive_port()
    split_contract.assert_engine_runner_contract_compatible()

    # Reuse v2's strict schema/provider adapters and same-fingerprint guards, but widen
    # their explicit profile set to the finite shard sizes before those closures install.
    split_contract.SPLIT_PROFILES = _all_split_profiles()
    split_contract._validate_core = _validate_core
    split_contract._validate_sections = _validate_sections
    split_contract._install_schema_and_validation_adapters()
    for count in range(1, max(staged._SECTION_COUNTS.values()) + 1):
        capacity._COMPLETION_TOKEN_BUDGETS[_sections_profile(count)] = _section_completion_tokens(count)

    split_contract._install_groq_model_diversity()
    split_contract._install_same_fingerprint_guard()
    _install_provider_attempt_budget_guard()
    _install_adaptive_call_state_machine()
    split_contract._install_plan_handoff_equivalence_guard()

    # Prevent a later accidental invocation of the legacy two-call-index installer in
    # the same process.  Production runtime calls only this adaptive owner.
    split_contract._INSTALLED = True
    _INSTALLED = True
    print(
        "Planning outline adaptive sharding v1 installed: "
        "fast_path=core+skeleton->sections_all fallback=failed_sections_only_8-4-2-1 "
        f"provider_attempt_hard_max={OUTLINE_PROVIDER_ATTEMPT_HARD_MAX} "
        "transient_or_tpm_window_sharding=false shard_retry_sweeps=1 "
        "global_skeleton_exact_id_purpose=true global_validation=fail_closed "
        "plan_json_exact_domain_projection=true quality_gates=unchanged"
    )

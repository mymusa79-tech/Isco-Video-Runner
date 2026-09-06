from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from enum import Enum

from scripts import planning_stage_contract as canonical
from scripts.orchestration_stage_registry import (
    CachePolicy,
    DeadlinePolicy,
    ImplementationBinding,
    RetryPolicy,
    StageContract,
    StageRegistry,
    StageRegistryError,
)


PLANNING_RESOLVER_ID = "planning-canonical-v1"
PLANNING_CONTRACT_SOURCE_PATH = "scripts/planning_stage_contract.py"
PLANNING_CONTRACT_SOURCE_SHA = "61c627b418d9148a944957f3dc821c3b8d1a28f4"


class PlanningRegistrationError(StageRegistryError):
    error_class = "INTERNAL_CONTRACT_ERROR"


class PlanningRequestKind(str, Enum):
    EDITORIAL_OUTLINE = "editorial_outline"
    FULL_SCRIPT = "full_script"
    SCRIPT_DOCTOR = "script_doctor"
    DOSSIER_REPAIR = "dossier_repair"
    APPEND_EXACT = "append_exact"
    APPEND_CANDIDATE = "append_candidate"
    SECTION_REPAIR = "section_repair"


@dataclass(frozen=True)
class PlanningRegistrationRequest:
    kind: PlanningRequestKind
    expected_count: int | None = None
    expected_ids: tuple[str, ...] = ()
    section_id: str | None = None


_ERROR_MAP = {
    canonical.PlanningErrorCode.PROVIDER_TRANSIENT: "TRANSIENT_PROVIDER",
    canonical.PlanningErrorCode.CAPACITY: "PROVIDER_CAPACITY",
    canonical.PlanningErrorCode.STRUCTURAL_INVALID: "INVALID_STRUCTURAL",
    canonical.PlanningErrorCode.SEMANTIC_INVALID: "INVALID_SEMANTIC",
    canonical.PlanningErrorCode.CHECKPOINT_INVALID: "INVALID_CACHE",
    canonical.PlanningErrorCode.INTERNAL_CONTRACT_ERROR: "INTERNAL_CONTRACT_ERROR",
}


_VERSION_RE = re.compile(r"\.v([1-9][0-9]*)$")


def map_planning_error_code(code: canonical.PlanningErrorCode) -> str:
    try:
        return _ERROR_MAP[code]
    except KeyError as exc:
        raise PlanningRegistrationError(f"unmapped canonical Planning error code: {code!r}") from exc


def _canonical_spec(request: PlanningRegistrationRequest) -> canonical.PlanningStageSpec:
    if not isinstance(request, PlanningRegistrationRequest):
        raise PlanningRegistrationError("Planning resolver requires PlanningRegistrationRequest")

    if request.kind is PlanningRequestKind.EDITORIAL_OUTLINE:
        if request.expected_count is None or request.expected_ids or request.section_id is not None:
            raise PlanningRegistrationError("editorial_outline requires expected_count only")
        return canonical.outline_stage_spec(request.expected_count)

    if request.kind in {
        PlanningRequestKind.FULL_SCRIPT,
        PlanningRequestKind.SCRIPT_DOCTOR,
        PlanningRequestKind.DOSSIER_REPAIR,
    }:
        if request.expected_count is not None or not request.expected_ids or request.section_id is not None:
            raise PlanningRegistrationError(f"{request.kind.value} requires expected_ids only")
        return canonical.script_stage_spec(request.kind.value, list(request.expected_ids))

    if request.kind in {PlanningRequestKind.APPEND_EXACT, PlanningRequestKind.APPEND_CANDIDATE}:
        if request.expected_count is not None or not request.expected_ids or request.section_id is not None:
            raise PlanningRegistrationError(f"{request.kind.value} requires expected_ids only")
        return canonical.append_stage_spec(
            list(request.expected_ids),
            allow_ordered_subset=request.kind is PlanningRequestKind.APPEND_CANDIDATE,
        )

    if request.kind is PlanningRequestKind.SECTION_REPAIR:
        if request.expected_count is not None or request.expected_ids or request.section_id is None:
            raise PlanningRegistrationError("section_repair requires section_id only")
        return canonical.section_repair_stage_spec(request.section_id)

    raise PlanningRegistrationError(f"unsupported explicit Planning request kind: {request.kind!r}")


def _contract_version(contract_id: str) -> int:
    match = _VERSION_RE.search(contract_id)
    if match is None:
        raise PlanningRegistrationError(
            f"canonical Planning contract_id has no explicit version suffix: {contract_id}"
        )
    return int(match.group(1))


def adapt_planning_spec(spec: canonical.PlanningStageSpec) -> StageContract:
    canonical_provider_policy = asdict(spec.provider_policy)
    canonical_cache_policy = asdict(spec.cache_policy)
    mapped_errors = tuple(map_planning_error_code(code) for code in canonical.PlanningErrorCode)

    return StageContract(
        stage_id=spec.stage_id,
        contract_id=spec.contract_id,
        contract_version=_contract_version(spec.contract_id),
        input_schema={
            "canonical_input": "effective_prompt",
            "canonical_binding": "planning_stage_contract.bind_request_contract",
        },
        input_hash_policy="canonical:sha256(effective_prompt.utf8)",
        output_schema=spec.output_schema,
        semantic_rules=spec.semantic_rules,
        provider_policy={
            "owner": "canonical-planning-stage-contract",
            "canonical_policy": canonical_provider_policy,
        },
        retry_policy=RetryPolicy(
            owner="canonical-planning-stage-contract",
            bounded=True,
            limit_source="canonical:PlanningStageSpec.provider_policy",
            backoff_policy="canonical:planning provider router",
            non_retryable=(
                "INVALID_STRUCTURAL",
                "INVALID_SEMANTIC",
                "INVALID_CONTRACT_INPUT",
                "INVALID_CACHE",
                "INTERNAL_CONTRACT_ERROR",
            ),
        ),
        cache_policy=CachePolicy(
            read=spec.cache_policy.read,
            write=spec.cache_policy.write,
            write_after_validation=True,
            revalidate_hits=spec.cache_policy.revalidate_on_hit,
            ttl_policy=f"canonical:{spec.cache_policy.namespace}",
            canonical_policy=canonical_cache_policy,
        ),
        deadline_policy=DeadlinePolicy(
            minimum_viable_ms="contract-owned:planning-stage-contract.minimum_viable_ms",
            local_cap_ms="contract-owned:planning-stage-contract.local_cap_ms",
            downstream_reserve_class="planning",
        ),
        side_effect_policy="idempotent",
        error_taxonomy=mapped_errors,
        evidence_schema="CanonicalPlanningStageContractEvidenceV1",
        implementation_binding=ImplementationBinding(
            adapter_id="planning-stage-contract-396-adapter",
            adapter_version=1,
            source_path=PLANNING_CONTRACT_SOURCE_PATH,
            source_sha=PLANNING_CONTRACT_SOURCE_SHA,
        ),
    )


def resolve_planning_registration(request: PlanningRegistrationRequest) -> StageContract:
    return adapt_planning_spec(_canonical_spec(request))


def register_planning_adapter(registry: StageRegistry) -> StageRegistry:
    registry.register_resolver(PLANNING_RESOLVER_ID, resolve_planning_registration)
    return registry

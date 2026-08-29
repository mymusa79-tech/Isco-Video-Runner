from __future__ import annotations

import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Callable, Iterable, Mapping


_ALLOWED_STAGE_ID = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$")
_ALLOWED_SHA = re.compile(r"^[0-9a-f]{40}$")


class FrozenList(tuple):
    """Tuple-backed marker that can be losslessly materialized back to a JSON list."""


def freeze_contract_data(value: object) -> object:
    """Freeze JSON-like contract metadata without dropping semantic fields."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: freeze_contract_data(item) for key, item in value.items()})
    if isinstance(value, list):
        return FrozenList(freeze_contract_data(item) for item in value)
    if isinstance(value, tuple):
        return tuple(freeze_contract_data(item) for item in value)
    if isinstance(value, set):
        return frozenset(freeze_contract_data(item) for item in value)
    return value


def materialize_contract_data(value: object) -> object:
    """Return ordinary Python containers for lossless invariant comparisons/adapters."""
    if isinstance(value, Mapping):
        return {key: materialize_contract_data(item) for key, item in value.items()}
    if isinstance(value, FrozenList):
        return [materialize_contract_data(item) for item in value]
    if isinstance(value, tuple):
        return tuple(materialize_contract_data(item) for item in value)
    if isinstance(value, frozenset):
        return {materialize_contract_data(item) for item in value}
    return value


def _has_contract_data(value: object) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (Mapping, tuple, list, set, frozenset)):
        return bool(value)
    return value is not None


ERROR_TAXONOMY = frozenset(
    {
        "TRANSIENT_PROVIDER",
        "PROVIDER_CAPACITY",
        "INVALID_STRUCTURAL",
        "INVALID_SEMANTIC",
        "INVALID_CONTRACT_INPUT",
        "INVALID_CACHE",
        "EXHAUSTED_DEADLINE",
        "SIDE_EFFECT_RECONCILIATION_REQUIRED",
        "INTERNAL_CONTRACT_ERROR",
    }
)

SIDE_EFFECT_POLICIES = frozenset({"none", "idempotent", "transactional", "compensatable"})


class StageRegistryError(ValueError):
    """Fail-loud contract/registry violation."""


@dataclass(frozen=True)
class RetryPolicy:
    owner: str
    bounded: bool = True
    max_attempts: int | None = None
    limit_source: str | None = None
    backoff_policy: str = "contract-owned"
    non_retryable: tuple[str, ...] = ()

    def validate(self) -> None:
        if not self.owner.strip():
            raise StageRegistryError("retry_policy.owner is required")
        if not self.bounded:
            raise StageRegistryError("retry_policy must be bounded")
        if self.max_attempts is not None and self.max_attempts < 1:
            raise StageRegistryError("retry_policy.max_attempts must be >= 1")
        if self.max_attempts is None and not (self.limit_source or "").strip():
            raise StageRegistryError("retry_policy needs max_attempts or explicit limit_source")
        unknown = set(self.non_retryable) - ERROR_TAXONOMY
        if unknown:
            raise StageRegistryError(f"illegal retry error taxonomy: {sorted(unknown)}")


@dataclass(frozen=True)
class CachePolicy:
    read: bool
    write: bool
    write_after_validation: bool
    revalidate_hits: bool
    ttl_policy: str = "contract-owned"
    canonical_policy: object | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "canonical_policy", freeze_contract_data(self.canonical_policy))

    def validate(self) -> None:
        if self.write and not self.write_after_validation:
            raise StageRegistryError("cache write before structural+semantic validation is forbidden")
        if self.read and not self.revalidate_hits:
            raise StageRegistryError("cache hits must be revalidated against the current contract")
        if not self.ttl_policy.strip():
            raise StageRegistryError("cache ttl/trust policy is required")


BudgetValue = int | str


def _validate_budget_value(value: BudgetValue, field_name: str) -> None:
    if isinstance(value, bool):
        raise StageRegistryError(f"deadline {field_name} must be a positive integer or contract-owned reference")
    if isinstance(value, int):
        if value <= 0:
            raise StageRegistryError(f"deadline {field_name} must be > 0")
        return
    if isinstance(value, str) and value.startswith("contract-owned:") and value.split(":", 1)[1].strip():
        return
    raise StageRegistryError(
        f"deadline {field_name} must be a positive integer or contract-owned reference"
    )


@dataclass(frozen=True)
class DeadlinePolicy:
    minimum_viable_ms: BudgetValue
    local_cap_ms: BudgetValue
    downstream_reserve_class: str

    def validate(self) -> None:
        _validate_budget_value(self.minimum_viable_ms, "minimum_viable_ms")
        _validate_budget_value(self.local_cap_ms, "local_cap_ms")
        if isinstance(self.minimum_viable_ms, int) and isinstance(self.local_cap_ms, int):
            if self.local_cap_ms < self.minimum_viable_ms:
                raise StageRegistryError("deadline local_cap_ms must be >= minimum_viable_ms")
        if not self.downstream_reserve_class.strip():
            raise StageRegistryError("deadline downstream reserve class is required")


@dataclass(frozen=True)
class ImplementationBinding:
    adapter_id: str
    adapter_version: int
    source_path: str
    source_sha: str

    def validate(self) -> None:
        if not self.adapter_id.strip():
            raise StageRegistryError("implementation adapter_id is required")
        if self.adapter_version < 1:
            raise StageRegistryError("implementation adapter_version must be >= 1")
        if not self.source_path.strip():
            raise StageRegistryError("implementation source_path is required")
        if not _ALLOWED_SHA.fullmatch(self.source_sha):
            raise StageRegistryError("implementation source_sha must be a full 40-char git blob SHA")


@dataclass(frozen=True)
class StageContract:
    stage_id: str
    contract_id: str
    contract_version: int
    input_schema: object
    input_hash_policy: str
    output_schema: object
    semantic_rules: object
    provider_policy: Mapping[str, object]
    retry_policy: RetryPolicy
    cache_policy: CachePolicy
    deadline_policy: DeadlinePolicy
    side_effect_policy: str
    error_taxonomy: tuple[str, ...]
    evidence_schema: str
    implementation_binding: ImplementationBinding

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_schema", freeze_contract_data(self.input_schema))
        object.__setattr__(self, "output_schema", freeze_contract_data(self.output_schema))
        object.__setattr__(self, "semantic_rules", freeze_contract_data(self.semantic_rules))
        object.__setattr__(self, "provider_policy", freeze_contract_data(self.provider_policy))
        object.__setattr__(self, "error_taxonomy", tuple(self.error_taxonomy))
        self.validate()

    def validate(self) -> None:
        if not _ALLOWED_STAGE_ID.fullmatch(self.stage_id):
            raise StageRegistryError(f"invalid stage_id: {self.stage_id!r}")
        if not self.contract_id.strip():
            raise StageRegistryError("contract_id is required")
        if self.contract_version < 1:
            raise StageRegistryError("contract_version must be >= 1")
        if not _has_contract_data(self.input_schema) or not _has_contract_data(self.output_schema):
            raise StageRegistryError("input_schema and output_schema are required")
        if not self.input_hash_policy.strip():
            raise StageRegistryError("input_hash_policy is required")
        if not _has_contract_data(self.semantic_rules):
            raise StageRegistryError("semantic_rules must be non-empty")
        owner = str(self.provider_policy.get("owner") or "").strip()
        if not owner:
            raise StageRegistryError("provider_policy.owner is required")
        self.retry_policy.validate()
        self.cache_policy.validate()
        self.deadline_policy.validate()
        if self.side_effect_policy not in SIDE_EFFECT_POLICIES:
            raise StageRegistryError(f"illegal side_effect_policy: {self.side_effect_policy}")
        if not self.error_taxonomy:
            raise StageRegistryError("error_taxonomy must be non-empty")
        unknown_errors = set(self.error_taxonomy) - ERROR_TAXONOMY
        if unknown_errors:
            raise StageRegistryError(f"illegal error taxonomy: {sorted(unknown_errors)}")
        if not self.evidence_schema.strip():
            raise StageRegistryError("evidence_schema is required")
        self.implementation_binding.validate()


StageProvider = Callable[[], Iterable[StageContract]]
StageResolver = Callable[..., StageContract]


@dataclass
class StageRegistry:
    _contracts: dict[str, StageContract] = field(default_factory=dict)
    _providers: dict[str, StageProvider] = field(default_factory=dict)
    _resolvers: dict[str, StageResolver] = field(default_factory=dict)

    def register(self, contract: StageContract) -> None:
        contract.validate()
        if contract.stage_id in self._contracts:
            raise StageRegistryError(f"duplicate stage_id: {contract.stage_id}")
        self._contracts[contract.stage_id] = contract

    def register_provider(self, provider_id: str, provider: StageProvider) -> None:
        key = provider_id.strip()
        if not key:
            raise StageRegistryError("provider_id is required")
        if key in self._providers:
            raise StageRegistryError(f"duplicate stage provider: {key}")
        self._providers[key] = provider

    def load_provider(self, provider_id: str) -> None:
        try:
            provider = self._providers[provider_id]
        except KeyError as exc:
            raise StageRegistryError(f"unknown stage provider: {provider_id}") from exc
        staged = tuple(provider())
        seen = set(self._contracts)
        for contract in staged:
            contract.validate()
            if contract.stage_id in seen:
                raise StageRegistryError(f"duplicate stage_id: {contract.stage_id}")
            seen.add(contract.stage_id)
        for contract in staged:
            self._contracts[contract.stage_id] = contract

    def register_resolver(self, resolver_id: str, resolver: StageResolver) -> None:
        key = resolver_id.strip()
        if not key:
            raise StageRegistryError("resolver_id is required")
        if key in self._resolvers:
            raise StageRegistryError(f"duplicate stage resolver: {key}")
        self._resolvers[key] = resolver

    def resolve(self, resolver_id: str, *args: object, **kwargs: object) -> StageContract:
        """Resolve a request-specific immutable contract without mutating registry state."""
        try:
            resolver = self._resolvers[resolver_id]
        except KeyError as exc:
            raise StageRegistryError(f"unknown stage resolver: {resolver_id}") from exc
        contract = resolver(*args, **kwargs)
        if not isinstance(contract, StageContract):
            raise StageRegistryError(f"resolver did not return StageContract: {resolver_id}")
        contract.validate()
        return contract

    def get(self, stage_id: str) -> StageContract:
        try:
            return self._contracts[stage_id]
        except KeyError as exc:
            raise StageRegistryError(f"unknown stage_id: {stage_id}") from exc

    def freeze_for_run(self) -> Mapping[str, StageContract]:
        """Immutable per-run snapshot; later registry mutation cannot affect this run."""
        return MappingProxyType(dict(self._contracts))

    def stage_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._contracts))


_COMMON_ERRORS = (
    "TRANSIENT_PROVIDER",
    "PROVIDER_CAPACITY",
    "INVALID_STRUCTURAL",
    "INVALID_SEMANTIC",
    "INVALID_CONTRACT_INPUT",
    "INVALID_CACHE",
    "EXHAUSTED_DEADLINE",
    "INTERNAL_CONTRACT_ERROR",
)


def _contract(
    *,
    stage_id: str,
    input_schema: str,
    output_schema: str,
    semantic_rules: tuple[str, ...],
    provider_owner: str,
    retry_owner: str,
    cache_read: bool,
    cache_write: bool,
    minimum_viable_ms: BudgetValue,
    local_cap_ms: BudgetValue,
    reserve_class: str,
    side_effect_policy: str,
    evidence_schema: str,
    adapter_id: str,
    source_path: str,
    source_sha: str,
) -> StageContract:
    return StageContract(
        stage_id=stage_id,
        contract_id=f"{stage_id}-stage-v1",
        contract_version=1,
        input_schema=input_schema,
        input_hash_policy="semantic-canonical-v1",
        output_schema=output_schema,
        semantic_rules=semantic_rules,
        provider_policy={"owner": provider_owner, "mode": "preserve-certified-core"},
        retry_policy=RetryPolicy(
            owner=retry_owner,
            bounded=True,
            limit_source="certified-core-policy",
            backoff_policy="certified-core-policy",
            non_retryable=("INVALID_STRUCTURAL", "INVALID_CONTRACT_INPUT", "INTERNAL_CONTRACT_ERROR"),
        ),
        cache_policy=CachePolicy(
            read=cache_read,
            write=cache_write,
            write_after_validation=True,
            revalidate_hits=True,
            ttl_policy="certified-core-policy",
        ),
        deadline_policy=DeadlinePolicy(
            minimum_viable_ms=minimum_viable_ms,
            local_cap_ms=local_cap_ms,
            downstream_reserve_class=reserve_class,
        ),
        side_effect_policy=side_effect_policy,
        error_taxonomy=_COMMON_ERRORS,
        evidence_schema=evidence_schema,
        implementation_binding=ImplementationBinding(
            adapter_id=adapter_id,
            adapter_version=1,
            source_path=source_path,
            source_sha=source_sha,
        ),
    )


def certified_non_planning_contracts() -> tuple[StageContract, ...]:
    """L4 metadata only. No stage execution or production wiring occurs here."""
    return (
        _contract(
            stage_id="tts",
            input_schema="TTSStageInputV1",
            output_schema="TTSStageOutputV1",
            semantic_rules=(
                "validated_plan_sections",
                "voice_runtime_identity_bound",
                "audio_semantic_integrity_preserved",
                "durable_cache_candidate_revalidated",
            ),
            provider_owner="legacy-voice-mesh-core",
            retry_owner="legacy-voice-mesh-core",
            cache_read=True,
            cache_write=True,
            minimum_viable_ms="contract-owned:tts.minimum_viable_ms",
            local_cap_ms="contract-owned:tts.local_cap_ms",
            reserve_class="compute",
            side_effect_policy="idempotent",
            evidence_schema="TTSEvidenceV1",
            adapter_id="legacy-tts-binding",
            source_path="scripts/run_v3_voice.py",
            source_sha="4fdbb64697f75c96acfea4b40c226056a8210524",
        ),
        _contract(
            stage_id="media",
            input_schema="MediaStageInputV1",
            output_schema="MediaStageOutputV1",
            semantic_rules=(
                "visual_intents_bound",
                "trusted_bytes_only",
                "current_security_recheck",
                "provenance_preserved",
            ),
            provider_owner="media-trust-security-core",
            retry_owner="media-trust-security-core",
            cache_read=True,
            cache_write=True,
            minimum_viable_ms="contract-owned:media.minimum_viable_ms",
            local_cap_ms="contract-owned:media.local_cap_ms",
            reserve_class="compute",
            side_effect_policy="idempotent",
            evidence_schema="MediaEvidenceV1",
            adapter_id="media-trust-boundary-v2",
            source_path="scripts/media_trust_boundary_v2.py",
            source_sha="c4a50233dbda9b0b1e920d3db1a1eb3ff58cb32d",
        ),
        _contract(
            stage_id="cinematic",
            input_schema="CinematicStageInputV1",
            output_schema="CinematicStageOutputV1",
            semantic_rules=(
                "trusted_media_only",
                "timeline_plan_bound",
                "capability_manifest_drives_required_optional",
                "existing_m8_m9_m10_m11_sfx_cta_semantics_preserved",
            ),
            provider_owner="certified-cinematic-core",
            retry_owner="certified-cinematic-core",
            cache_read=False,
            cache_write=False,
            minimum_viable_ms="contract-owned:cinematic.minimum_viable_ms",
            local_cap_ms="contract-owned:cinematic.local_cap_ms",
            reserve_class="compute",
            side_effect_policy="none",
            evidence_schema="CinematicEvidenceV1",
            adapter_id="m7-cinematic-live-binding",
            source_path="scripts/m7_live_binding.py",
            source_sha="0f00dfb316c5fc1eeea0f7ccca577d9bc0a5dd94",
        ),
        _contract(
            stage_id="render",
            input_schema="RenderStageInputV1",
            output_schema="RenderStageOutputV1",
            semantic_rules=(
                "prepared_picture_audio_subtitles_cards",
                "content_addressed_render_identity",
                "render_cache_candidate_revalidated",
            ),
            provider_owner="render-durable-core",
            retry_owner="render-durable-core",
            cache_read=True,
            cache_write=True,
            minimum_viable_ms="contract-owned:render.minimum_viable_ms",
            local_cap_ms="contract-owned:render.local_cap_ms",
            reserve_class="compute",
            side_effect_policy="idempotent",
            evidence_schema="RenderEvidenceV1",
            adapter_id="render-durable-cache",
            source_path="scripts/render_durable_cache.py",
            source_sha="abc92b472373cada7b92a7a53007ae943de98b27",
        ),
        _contract(
            stage_id="qc",
            input_schema="QCStageInputV1",
            output_schema="QCStageOutputV1",
            semantic_rules=(
                "exact_final_render_validated",
                "semantic_artifacts_bound",
                "final_master_qc_authority_preserved",
                "observers_non_authoritative",
            ),
            provider_owner="final-master-qc-core",
            retry_owner="final-master-qc-core",
            cache_read=False,
            cache_write=False,
            minimum_viable_ms="contract-owned:qc.minimum_viable_ms",
            local_cap_ms="contract-owned:qc.local_cap_ms",
            reserve_class="validation",
            side_effect_policy="none",
            evidence_schema="QCEvidenceV1",
            adapter_id="final-master-qc",
            source_path="scripts/final_master_qc.py",
            source_sha="e3412fc5710618eb9d7529710d8dbbc539e9fa91",
        ),
        _contract(
            stage_id="shorts",
            input_schema="ShortsStageInputV1",
            output_schema="ShortsStageOutputV1",
            semantic_rules=(
                "accepted_long_source_bound",
                "sibling_plan_validated",
                "canonical_bundle_child_design_preserved",
                "delivery_evidence_required",
            ),
            provider_owner="canonical-short-child-core",
            retry_owner="canonical-short-child-core",
            cache_read=False,
            cache_write=False,
            minimum_viable_ms="contract-owned:shorts.minimum_viable_ms",
            local_cap_ms="contract-owned:shorts.local_cap_ms",
            reserve_class="compute",
            side_effect_policy="idempotent",
            evidence_schema="ShortsEvidenceV1",
            adapter_id="shorts-production-binding",
            source_path="scripts/shorts_production_binding.py",
            source_sha="48043498da00b320b41f255cde544253db2ccb77",
        ),
    )


def build_l4_registry() -> StageRegistry:
    registry = StageRegistry()
    registry.register_provider("certified-non-planning-v1", certified_non_planning_contracts)
    registry.load_provider("certified-non-planning-v1")
    return registry

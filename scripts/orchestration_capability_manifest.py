from __future__ import annotations

"""Production Capability Manifest foundation.

The manifest answers composition conformance separately from Final Master QC. A
capability's requirement is decided before its outcome is recorded. Required
capabilities are release-eligible only when applied. Any requirement exception must be
typed and predeclared by the capability contract; it cannot be invented after failure.
"""

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from typing import Callable, Iterable


SCHEMA_VERSION = 1


class CapabilityContractError(ValueError):
    """Raised when capability composition violates its declared contract."""


class CapabilityRequirement(str, Enum):
    REQUIRED = "required"
    OPTIONAL = "optional"


class CapabilityState(str, Enum):
    APPLIED = "applied"
    NOT_APPLICABLE = "not_applicable"
    DEGRADED_ACCEPTED = "degraded_accepted"
    BLOCKED = "blocked"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RequirementException:
    code: str
    effective_requirement: CapabilityRequirement

    def __post_init__(self) -> None:
        _require_token("requirement exception code", self.code)


@dataclass(frozen=True, slots=True)
class CapabilityContract:
    capability_id: str
    contract_id: str
    validator_version: str
    default_requirement: CapabilityRequirement
    allowed_optional_states: frozenset[CapabilityState]
    allowed_reason_codes: frozenset[str]
    requirement_exceptions: tuple[RequirementException, ...] = ()

    def __post_init__(self) -> None:
        _require_token("capability_id", self.capability_id)
        _require_token("contract_id", self.contract_id)
        _require_token("validator_version", self.validator_version)
        if not self.allowed_reason_codes:
            raise CapabilityContractError("allowed_reason_codes must not be empty")
        for code in self.allowed_reason_codes:
            _require_token("reason code", code)
        duplicate_exception_codes = _duplicates(exc.code for exc in self.requirement_exceptions)
        if duplicate_exception_codes:
            raise CapabilityContractError(
                f"duplicate requirement exception code:{sorted(duplicate_exception_codes)}"
            )
        for state in self.allowed_optional_states:
            if state in {CapabilityState.BLOCKED, CapabilityState.FAILED}:
                raise CapabilityContractError(
                    "blocked/failed cannot be release-eligible optional states"
                )

    def effective_requirement(
        self, exception_code: str | None
    ) -> CapabilityRequirement:
        if exception_code is None:
            return self.default_requirement
        for exc in self.requirement_exceptions:
            if exc.code == exception_code:
                return exc.effective_requirement
        raise CapabilityContractError(
            f"undeclared requirement exception:{self.capability_id}:{exception_code}"
        )


@dataclass(frozen=True, slots=True)
class CapabilityEntry:
    capability_id: str
    requirement: CapabilityRequirement
    state: CapabilityState
    reason_code: str
    evidence_refs: tuple[str, ...]
    contract_id: str
    validator_version: str
    requirement_exception_code: str | None = None


@dataclass(frozen=True, slots=True)
class CapabilityManifest:
    schema_version: int
    run_id: str
    entries: tuple[CapabilityEntry, ...]

    def canonical_payload(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "capabilities": [
                {
                    "capability_id": entry.capability_id,
                    "requirement": entry.requirement.value,
                    "state": entry.state.value,
                    "reason_code": entry.reason_code,
                    "evidence_refs": list(entry.evidence_refs),
                    "contract_id": entry.contract_id,
                    "validator_version": entry.validator_version,
                    "requirement_exception_code": entry.requirement_exception_code,
                }
                for entry in sorted(self.entries, key=lambda item: item.capability_id)
            ],
        }

    def canonical_json(self) -> str:
        return json.dumps(
            self.canonical_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ReleaseEligibility:
    eligible: bool
    manifest_sha256: str
    blocker_capability_ids: tuple[str, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Expectation:
    requirement: CapabilityRequirement
    exception_code: str | None


@dataclass(frozen=True, slots=True)
class _Outcome:
    state: CapabilityState
    reason_code: str
    evidence_refs: tuple[str, ...]


EvidenceValidator = Callable[[str], bool]


def _default_evidence_validator(ref: str) -> bool:
    return isinstance(ref, str) and bool(ref.strip())


class CapabilityManifestBuilder:
    """Two-phase builder: declare requirement first, then record outcome."""

    def __init__(self, run_id: str, contracts: Iterable[CapabilityContract]) -> None:
        _require_token("run_id", run_id)
        materialized = tuple(contracts)
        if not materialized:
            raise CapabilityContractError("at least one capability contract is required")
        duplicates = _duplicates(contract.capability_id for contract in materialized)
        if duplicates:
            raise CapabilityContractError(
                f"duplicate capability contract:{sorted(duplicates)}"
            )
        self._run_id = run_id
        self._contracts = {contract.capability_id: contract for contract in materialized}
        self._expectations: dict[str, _Expectation] = {}
        self._outcomes: dict[str, _Outcome] = {}

    def declare_requirement(
        self,
        capability_id: str,
        *,
        requirement_exception_code: str | None = None,
    ) -> CapabilityRequirement:
        contract = self._contract(capability_id)
        if capability_id in self._outcomes:
            raise CapabilityContractError(
                f"requirement cannot change after outcome:{capability_id}"
            )
        effective = contract.effective_requirement(requirement_exception_code)
        expectation = _Expectation(
            requirement=effective,
            exception_code=requirement_exception_code,
        )
        existing = self._expectations.get(capability_id)
        if existing is not None and existing != expectation:
            raise CapabilityContractError(
                f"requirement already frozen:{capability_id}"
            )
        self._expectations[capability_id] = expectation
        return effective

    def record_outcome(
        self,
        capability_id: str,
        *,
        state: CapabilityState,
        reason_code: str,
        evidence_refs: tuple[str, ...],
    ) -> None:
        contract = self._contract(capability_id)
        if capability_id not in self._expectations:
            raise CapabilityContractError(
                f"requirement must be declared before outcome:{capability_id}"
            )
        if capability_id in self._outcomes:
            raise CapabilityContractError(f"outcome already recorded:{capability_id}")
        if reason_code not in contract.allowed_reason_codes:
            raise CapabilityContractError(
                f"reason code not allowed by contract:{capability_id}:{reason_code}"
            )
        if not evidence_refs:
            raise CapabilityContractError(
                f"capability outcome requires evidence_refs:{capability_id}"
            )
        if any(not isinstance(ref, str) or not ref.strip() for ref in evidence_refs):
            raise CapabilityContractError(
                f"invalid capability evidence ref:{capability_id}"
            )
        self._outcomes[capability_id] = _Outcome(
            state=state,
            reason_code=reason_code,
            evidence_refs=evidence_refs,
        )

    def build(self) -> CapabilityManifest:
        missing_declarations = sorted(set(self._contracts) - set(self._expectations))
        missing_outcomes = sorted(set(self._contracts) - set(self._outcomes))
        if missing_declarations or missing_outcomes:
            raise CapabilityContractError(
                "incomplete capability manifest:"
                f"missing_declarations={missing_declarations};"
                f"missing_outcomes={missing_outcomes}"
            )
        entries: list[CapabilityEntry] = []
        for capability_id in sorted(self._contracts):
            contract = self._contracts[capability_id]
            expectation = self._expectations[capability_id]
            outcome = self._outcomes[capability_id]
            entries.append(
                CapabilityEntry(
                    capability_id=capability_id,
                    requirement=expectation.requirement,
                    state=outcome.state,
                    reason_code=outcome.reason_code,
                    evidence_refs=outcome.evidence_refs,
                    contract_id=contract.contract_id,
                    validator_version=contract.validator_version,
                    requirement_exception_code=expectation.exception_code,
                )
            )
        return CapabilityManifest(
            schema_version=SCHEMA_VERSION,
            run_id=self._run_id,
            entries=tuple(entries),
        )

    def _contract(self, capability_id: str) -> CapabilityContract:
        try:
            return self._contracts[capability_id]
        except KeyError as exc:
            raise CapabilityContractError(
                f"unknown capability_id:{capability_id}"
            ) from exc


def evaluate_release_eligibility(
    manifest: CapabilityManifest,
    contracts: Iterable[CapabilityContract],
    *,
    evidence_validator: EvidenceValidator = _default_evidence_validator,
) -> ReleaseEligibility:
    if manifest.schema_version != SCHEMA_VERSION:
        raise CapabilityContractError(
            f"unsupported manifest schema_version:{manifest.schema_version}"
        )
    contract_map = {contract.capability_id: contract for contract in contracts}
    if len(contract_map) == 0:
        raise CapabilityContractError("release gate requires capability contracts")
    entry_map = {entry.capability_id: entry for entry in manifest.entries}
    if len(entry_map) != len(manifest.entries):
        raise CapabilityContractError("duplicate capability entries")
    if set(entry_map) != set(contract_map):
        raise CapabilityContractError(
            "manifest capability set does not match declared contracts"
        )

    blockers: list[str] = []
    reasons: list[str] = []
    for capability_id in sorted(contract_map):
        contract = contract_map[capability_id]
        entry = entry_map[capability_id]
        effective = contract.effective_requirement(entry.requirement_exception_code)
        if entry.requirement is not effective:
            raise CapabilityContractError(
                f"requirement drift:{capability_id}:{entry.requirement}!={effective}"
            )
        if entry.contract_id != contract.contract_id:
            raise CapabilityContractError(f"contract_id drift:{capability_id}")
        if entry.validator_version != contract.validator_version:
            raise CapabilityContractError(f"validator_version drift:{capability_id}")
        if entry.reason_code not in contract.allowed_reason_codes:
            raise CapabilityContractError(f"reason code drift:{capability_id}")
        if not entry.evidence_refs or not all(
            evidence_validator(ref) for ref in entry.evidence_refs
        ):
            blockers.append(capability_id)
            reasons.append(f"{capability_id}:EVIDENCE_INVALID")
            continue

        if entry.requirement is CapabilityRequirement.REQUIRED:
            if entry.state is not CapabilityState.APPLIED:
                blockers.append(capability_id)
                reasons.append(
                    f"{capability_id}:REQUIRED_NOT_APPLIED:{entry.state.value}"
                )
            continue

        if entry.state is CapabilityState.APPLIED:
            continue
        if entry.state in contract.allowed_optional_states:
            continue
        blockers.append(capability_id)
        reasons.append(
            f"{capability_id}:OPTIONAL_STATE_NOT_ALLOWED:{entry.state.value}"
        )

    return ReleaseEligibility(
        eligible=not blockers,
        manifest_sha256=manifest.sha256,
        blocker_capability_ids=tuple(blockers),
        reasons=tuple(reasons),
    )


def _require_token(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise CapabilityContractError(f"{name} must be a non-empty string")
    if any(ch.isspace() for ch in value):
        raise CapabilityContractError(f"{name} must be a typed token, not free text")


def _duplicates(values: Iterable[str]) -> set[str]:
    seen: set[str] = set()
    duplicate: set[str] = set()
    for value in values:
        if value in seen:
            duplicate.add(value)
        seen.add(value)
    return duplicate

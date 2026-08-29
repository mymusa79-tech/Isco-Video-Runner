from __future__ import annotations

import unittest

from scripts.orchestration_capability_manifest import (
    CapabilityContract,
    CapabilityContractError,
    CapabilityManifestBuilder,
    CapabilityRequirement,
    CapabilityState,
    RequirementException,
    evaluate_release_eligibility,
)


def required_contract(capability_id: str = "M8_COLOR_NORMALIZATION") -> CapabilityContract:
    return CapabilityContract(
        capability_id=capability_id,
        contract_id=f"{capability_id.lower()}-v1",
        validator_version="validator-v1",
        default_requirement=CapabilityRequirement.REQUIRED,
        allowed_optional_states=frozenset(
            {CapabilityState.NOT_APPLICABLE, CapabilityState.SKIPPED}
        ),
        allowed_reason_codes=frozenset(
            {"APPLIED_OK", "POLICY_NOT_REQUIRED", "POLICY_SKIP_ALLOWED", "EXECUTION_FAILED"}
        ),
        requirement_exceptions=(
            RequirementException(
                code="NO_STOCK_VIDEO",
                effective_requirement=CapabilityRequirement.OPTIONAL,
            ),
        ),
    )


def build_one(
    *,
    exception: str | None = None,
    state: CapabilityState = CapabilityState.APPLIED,
    reason: str = "APPLIED_OK",
):
    contract = required_contract()
    builder = CapabilityManifestBuilder("run-1", [contract])
    builder.declare_requirement(
        contract.capability_id, requirement_exception_code=exception
    )
    builder.record_outcome(
        contract.capability_id,
        state=state,
        reason_code=reason,
        evidence_refs=("journal:42",),
    )
    return contract, builder.build()


class CapabilityContractTests(unittest.TestCase):
    def test_contract_rejects_free_text_reason_tokens_and_release_unsafe_optional_states(self) -> None:
        with self.assertRaises(CapabilityContractError):
            CapabilityContract(
                capability_id="M8",
                contract_id="m8-v1",
                validator_version="v1",
                default_requirement=CapabilityRequirement.OPTIONAL,
                allowed_optional_states=frozenset({CapabilityState.FAILED}),
                allowed_reason_codes=frozenset({"free text reason"}),
            )

    def test_undeclared_requirement_exception_is_rejected(self) -> None:
        contract = required_contract()
        builder = CapabilityManifestBuilder("run-1", [contract])
        with self.assertRaises(CapabilityContractError):
            builder.declare_requirement(
                contract.capability_id, requirement_exception_code="INVENTED_AFTER_FAILURE"
            )


class CapabilityBuilderTests(unittest.TestCase):
    def test_requirement_must_be_declared_before_outcome(self) -> None:
        contract = required_contract()
        builder = CapabilityManifestBuilder("run-1", [contract])
        with self.assertRaises(CapabilityContractError):
            builder.record_outcome(
                contract.capability_id,
                state=CapabilityState.APPLIED,
                reason_code="APPLIED_OK",
                evidence_refs=("journal:1",),
            )

    def test_requirement_cannot_change_after_outcome(self) -> None:
        contract = required_contract()
        builder = CapabilityManifestBuilder("run-1", [contract])
        builder.declare_requirement(contract.capability_id)
        builder.record_outcome(
            contract.capability_id,
            state=CapabilityState.FAILED,
            reason_code="EXECUTION_FAILED",
            evidence_refs=("journal:2",),
        )
        with self.assertRaises(CapabilityContractError):
            builder.declare_requirement(
                contract.capability_id, requirement_exception_code="NO_STOCK_VIDEO"
            )

    def test_manifest_requires_every_declared_capability_outcome(self) -> None:
        first = required_contract("M8")
        second = required_contract("M9")
        builder = CapabilityManifestBuilder("run-1", [first, second])
        builder.declare_requirement("M8")
        builder.record_outcome(
            "M8",
            state=CapabilityState.APPLIED,
            reason_code="APPLIED_OK",
            evidence_refs=("journal:3",),
        )
        with self.assertRaises(CapabilityContractError):
            builder.build()

    def test_outcome_requires_contract_typed_reason_and_evidence(self) -> None:
        contract = required_contract()
        builder = CapabilityManifestBuilder("run-1", [contract])
        builder.declare_requirement(contract.capability_id)
        with self.assertRaises(CapabilityContractError):
            builder.record_outcome(
                contract.capability_id,
                state=CapabilityState.APPLIED,
                reason_code="UNKNOWN_REASON",
                evidence_refs=("journal:4",),
            )
        with self.assertRaises(CapabilityContractError):
            builder.record_outcome(
                contract.capability_id,
                state=CapabilityState.APPLIED,
                reason_code="APPLIED_OK",
                evidence_refs=(),
            )


class ReleaseEligibilityTests(unittest.TestCase):
    def test_required_applied_with_valid_evidence_is_eligible(self) -> None:
        contract, manifest = build_one()
        result = evaluate_release_eligibility(manifest, [contract])
        self.assertTrue(result.eligible)
        self.assertEqual(result.blocker_capability_ids, ())
        self.assertEqual(result.manifest_sha256, manifest.sha256)

    def test_required_non_applied_states_block_release(self) -> None:
        for state in (
            CapabilityState.NOT_APPLICABLE,
            CapabilityState.DEGRADED_ACCEPTED,
            CapabilityState.BLOCKED,
            CapabilityState.SKIPPED,
            CapabilityState.FAILED,
        ):
            with self.subTest(state=state):
                contract, manifest = build_one(
                    state=state,
                    reason="EXECUTION_FAILED" if state is CapabilityState.FAILED else "POLICY_SKIP_ALLOWED",
                )
                result = evaluate_release_eligibility(manifest, [contract])
                self.assertFalse(result.eligible)
                self.assertEqual(result.blocker_capability_ids, (contract.capability_id,))

    def test_predeclared_exception_can_make_capability_optional_before_execution(self) -> None:
        contract, manifest = build_one(
            exception="NO_STOCK_VIDEO",
            state=CapabilityState.NOT_APPLICABLE,
            reason="POLICY_NOT_REQUIRED",
        )
        self.assertEqual(manifest.entries[0].requirement, CapabilityRequirement.OPTIONAL)
        result = evaluate_release_eligibility(manifest, [contract])
        self.assertTrue(result.eligible)

    def test_optional_skip_needs_explicit_contract_policy(self) -> None:
        contract, manifest = build_one(
            exception="NO_STOCK_VIDEO",
            state=CapabilityState.SKIPPED,
            reason="POLICY_SKIP_ALLOWED",
        )
        self.assertTrue(evaluate_release_eligibility(manifest, [contract]).eligible)

        strict_contract = CapabilityContract(
            capability_id="M8_COLOR_NORMALIZATION",
            contract_id="m8_color_normalization-v1",
            validator_version="validator-v1",
            default_requirement=CapabilityRequirement.REQUIRED,
            allowed_optional_states=frozenset({CapabilityState.NOT_APPLICABLE}),
            allowed_reason_codes=contract.allowed_reason_codes,
            requirement_exceptions=contract.requirement_exceptions,
        )
        strict_result = evaluate_release_eligibility(manifest, [strict_contract])
        self.assertFalse(strict_result.eligible)

    def test_invalid_evidence_blocks_release(self) -> None:
        contract, manifest = build_one()
        result = evaluate_release_eligibility(
            manifest,
            [contract],
            evidence_validator=lambda ref: ref != "journal:42",
        )
        self.assertFalse(result.eligible)
        self.assertIn("EVIDENCE_INVALID", result.reasons[0])

    def test_manifest_hash_is_deterministic_independent_of_contract_order(self) -> None:
        a = required_contract("A")
        b = required_contract("B")

        def make(contracts):
            builder = CapabilityManifestBuilder("run-1", contracts)
            for capability_id in ("B", "A"):
                builder.declare_requirement(capability_id)
                builder.record_outcome(
                    capability_id,
                    state=CapabilityState.APPLIED,
                    reason_code="APPLIED_OK",
                    evidence_refs=(f"journal:{capability_id}",),
                )
            return builder.build()

        self.assertEqual(make([a, b]).sha256, make([b, a]).sha256)


if __name__ == "__main__":
    unittest.main()

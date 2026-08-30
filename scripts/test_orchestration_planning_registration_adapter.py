import hashlib
import unittest
from dataclasses import asdict
from pathlib import Path

from scripts import planning_stage_contract as canonical
from scripts.orchestration_planning_registration_adapter import (
    PLANNING_CONTRACT_SOURCE_PATH,
    PLANNING_CONTRACT_SOURCE_SHA,
    PLANNING_RESOLVER_ID,
    PlanningRegistrationError,
    PlanningRegistrationRequest,
    PlanningRequestKind,
    map_planning_error_code,
    register_planning_adapter,
)
from scripts.orchestration_stage_registry import build_l4_registry, materialize_contract_data


class PlanningRegistrationAdapterTests(unittest.TestCase):
    def _resolve(self, request):
        registry = register_planning_adapter(build_l4_registry())
        return registry.resolve(PLANNING_RESOLVER_ID, request)

    def _assert_canonical_parity(self, general, spec):
        self.assertEqual(general.stage_id, spec.stage_id)
        self.assertEqual(general.contract_id, spec.contract_id)
        self.assertEqual(materialize_contract_data(general.output_schema), spec.output_schema)
        self.assertEqual(materialize_contract_data(general.semantic_rules), spec.semantic_rules)
        provider = materialize_contract_data(general.provider_policy)
        self.assertEqual(provider["canonical_policy"], asdict(spec.provider_policy))
        self.assertEqual(
            materialize_contract_data(general.cache_policy.canonical_policy),
            asdict(spec.cache_policy),
        )
        self.assertEqual(general.cache_policy.read, spec.cache_policy.read)
        self.assertEqual(general.cache_policy.write, spec.cache_policy.write)
        self.assertEqual(general.cache_policy.revalidate_hits, spec.cache_policy.revalidate_on_hit)

    def test_adapter_registration_does_not_mutate_l4_static_stage_set(self):
        registry = build_l4_registry()
        before = registry.stage_ids()
        register_planning_adapter(registry)
        self.assertEqual(registry.stage_ids(), before)
        self.assertNotIn("planning", registry.stage_ids())

    def test_outline_contract_is_consumed_from_canonical_factory(self):
        request = PlanningRegistrationRequest(PlanningRequestKind.EDITORIAL_OUTLINE, expected_count=3)
        general = self._resolve(request)
        self._assert_canonical_parity(general, canonical.outline_stage_spec(3))

    def test_full_script_contract_is_consumed_from_canonical_factory(self):
        ids = ("s1", "s2", "s3")
        general = self._resolve(
            PlanningRegistrationRequest(PlanningRequestKind.FULL_SCRIPT, expected_ids=ids)
        )
        self._assert_canonical_parity(general, canonical.script_stage_spec("full_script", list(ids)))

    def test_script_doctor_contract_is_consumed_from_canonical_factory(self):
        ids = ("s1", "s2")
        general = self._resolve(
            PlanningRegistrationRequest(PlanningRequestKind.SCRIPT_DOCTOR, expected_ids=ids)
        )
        self._assert_canonical_parity(general, canonical.script_stage_spec("script_doctor", list(ids)))

    def test_dossier_repair_contract_is_consumed_from_canonical_factory(self):
        ids = ("s1", "s2")
        general = self._resolve(
            PlanningRegistrationRequest(PlanningRequestKind.DOSSIER_REPAIR, expected_ids=ids)
        )
        self._assert_canonical_parity(
            general,
            canonical.script_stage_spec("dossier_repair", list(ids)),
        )

    def test_append_exact_contract_preserves_no_fragment_cache(self):
        ids = ("s1", "s2")
        general = self._resolve(
            PlanningRegistrationRequest(PlanningRequestKind.APPEND_EXACT, expected_ids=ids)
        )
        spec = canonical.append_stage_spec(list(ids), allow_ordered_subset=False)
        self._assert_canonical_parity(general, spec)
        self.assertFalse(general.cache_policy.read)
        self.assertFalse(general.cache_policy.write)

    def test_append_candidate_contract_preserves_ordered_subset_semantics(self):
        ids = ("s1", "s2", "s3")
        general = self._resolve(
            PlanningRegistrationRequest(PlanningRequestKind.APPEND_CANDIDATE, expected_ids=ids)
        )
        spec = canonical.append_stage_spec(list(ids), allow_ordered_subset=True)
        self._assert_canonical_parity(general, spec)

    def test_section_repair_contract_is_consumed_from_canonical_factory(self):
        general = self._resolve(
            PlanningRegistrationRequest(PlanningRequestKind.SECTION_REPAIR, section_id="s9")
        )
        self._assert_canonical_parity(general, canonical.section_repair_stage_spec("s9"))

    def test_all_canonical_error_codes_have_generic_mapping(self):
        expected = {
            "TRANSIENT_PROVIDER",
            "PROVIDER_CAPACITY",
            "INVALID_STRUCTURAL",
            "INVALID_SEMANTIC",
            "INVALID_CACHE",
            "INTERNAL_CONTRACT_ERROR",
        }
        self.assertEqual({map_planning_error_code(code) for code in canonical.PlanningErrorCode}, expected)

    def test_provider_retry_ownership_remains_canonical_planning(self):
        general = self._resolve(
            PlanningRegistrationRequest(PlanningRequestKind.EDITORIAL_OUTLINE, expected_count=2)
        )
        self.assertEqual(general.provider_policy["owner"], "canonical-planning-stage-contract")
        self.assertEqual(general.retry_policy.owner, "canonical-planning-stage-contract")
        self.assertEqual(general.retry_policy.limit_source, "canonical:PlanningStageSpec.provider_policy")

    def test_adapter_binds_exact_protected_source_without_modifying_it(self):
        general = self._resolve(
            PlanningRegistrationRequest(PlanningRequestKind.EDITORIAL_OUTLINE, expected_count=2)
        )
        self.assertEqual(general.implementation_binding.source_path, PLANNING_CONTRACT_SOURCE_PATH)
        self.assertEqual(general.implementation_binding.source_sha, PLANNING_CONTRACT_SOURCE_SHA)
        source = Path(PLANNING_CONTRACT_SOURCE_PATH).read_bytes()
        actual_blob_sha = hashlib.sha1(
            f"blob {len(source)}\0".encode("ascii") + source
        ).hexdigest()
        self.assertEqual(actual_blob_sha, PLANNING_CONTRACT_SOURCE_SHA)

    def test_invalid_parameter_shapes_fail_closed(self):
        cases = (
            PlanningRegistrationRequest(PlanningRequestKind.EDITORIAL_OUTLINE),
            PlanningRegistrationRequest(
                PlanningRequestKind.EDITORIAL_OUTLINE, expected_count=2, expected_ids=("s1",)
            ),
            PlanningRegistrationRequest(PlanningRequestKind.FULL_SCRIPT),
            PlanningRegistrationRequest(PlanningRequestKind.DOSSIER_REPAIR),
            PlanningRegistrationRequest(
                PlanningRequestKind.FULL_SCRIPT, expected_ids=("s1",), section_id="s1"
            ),
            PlanningRegistrationRequest(PlanningRequestKind.APPEND_EXACT),
            PlanningRegistrationRequest(PlanningRequestKind.SECTION_REPAIR),
        )
        for request in cases:
            with self.subTest(request=request):
                with self.assertRaises(PlanningRegistrationError) as caught:
                    self._resolve(request)
                self.assertEqual(caught.exception.error_class, "INTERNAL_CONTRACT_ERROR")

    def test_explicit_request_kind_not_prompt_text_selects_contract(self):
        ids = ("s1", "s2")
        full = self._resolve(
            PlanningRegistrationRequest(PlanningRequestKind.FULL_SCRIPT, expected_ids=ids)
        )
        doctor = self._resolve(
            PlanningRegistrationRequest(PlanningRequestKind.SCRIPT_DOCTOR, expected_ids=ids)
        )
        self.assertEqual(full.stage_id, "planning.full_script")
        self.assertEqual(doctor.stage_id, "planning.script_doctor")
        self.assertNotEqual(full.contract_id, doctor.contract_id)


if __name__ == "__main__":
    unittest.main()

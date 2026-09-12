from __future__ import annotations

import unittest
from unittest import mock

from scripts import gold_final_critic_text_fallback as gold_text
from scripts import quality_capability_router as router
from scripts import quality_capability_runtime_binding as binding
from scripts import vision_stage_contract_v2 as vision


class QualityCapabilityRuntimeBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_primary = vision.OPENROUTER_PRIMARY_MODEL
        self.old_discover = vision._discover_alternate_free_vision_model
        self.old_gold_text = gold_text._OPENROUTER_MODEL
        binding.reset_quality_capability_runtime_binding_for_tests()

    def tearDown(self) -> None:
        vision.OPENROUTER_PRIMARY_MODEL = self.old_primary
        vision._discover_alternate_free_vision_model = self.old_discover
        gold_text._OPENROUTER_MODEL = self.old_gold_text
        binding.reset_quality_capability_runtime_binding_for_tests()

    def test_install_replaces_dynamic_aliases_with_exact_registry_models(self) -> None:
        binding.install_quality_capability_runtime_binding()
        vision_models = [
            item.model
            for item in router.policy_for(router.CAP_GOLD_VISION).candidates
            if item.provider == "openrouter"
        ]
        text_models = [
            item.model
            for item in router.policy_for(router.CAP_GOLD_TEXT).candidates
            if item.provider == "openrouter"
        ]
        self.assertEqual(vision.OPENROUTER_PRIMARY_MODEL, vision_models[0])
        self.assertEqual(gold_text._OPENROUTER_MODEL, text_models[0])
        self.assertNotEqual(vision.OPENROUTER_PRIMARY_MODEL, "openrouter/free")
        self.assertNotEqual(gold_text._OPENROUTER_MODEL, "openrouter/free")
        self.assertTrue(
            getattr(
                vision._discover_alternate_free_vision_model,
                "_isco_exact_quality_models_only",
                False,
            )
        )

    def test_alternate_is_selected_only_from_pinned_registry(self) -> None:
        binding.install_quality_capability_runtime_binding()
        vision_candidates = tuple(
            item
            for item in router.policy_for(router.CAP_GOLD_VISION).candidates
            if item.provider == "openrouter"
        )
        fake_decision = router.RouteDecision(
            router.CAP_GOLD_VISION,
            vision_candidates,
            (),
        )
        with mock.patch.object(router, "route_candidates", return_value=fake_decision):
            alternate = vision._discover_alternate_free_vision_model(
                exclude={vision_candidates[0].model}
            )
        self.assertEqual(alternate, vision_candidates[1].model)

    def test_no_random_discovery_when_all_pinned_models_excluded(self) -> None:
        binding.install_quality_capability_runtime_binding()
        vision_candidates = tuple(
            item
            for item in router.policy_for(router.CAP_GOLD_VISION).candidates
            if item.provider == "openrouter"
        )
        fake_decision = router.RouteDecision(
            router.CAP_GOLD_VISION,
            vision_candidates,
            (),
        )
        with mock.patch.object(router, "route_candidates", return_value=fake_decision):
            alternate = vision._discover_alternate_free_vision_model(
                exclude={item.model for item in vision_candidates}
            )
        self.assertIsNone(alternate)


if __name__ == "__main__":
    unittest.main()

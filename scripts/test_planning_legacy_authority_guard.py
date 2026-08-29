from __future__ import annotations

import unittest
from unittest import mock

from scripts import planning_legacy_authority_guard as guard
from scripts import planning_stage_contract as stage_contract
from scripts import task_level_planner_router as legacy_router


class PlanningLegacyAuthorityGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_schema_resolver = legacy_router._structured_schema_for_prompt
        self.original_load = legacy_router._load_checkpoint
        self.original_save = legacy_router._save_checkpoint

    def tearDown(self) -> None:
        legacy_router._structured_schema_for_prompt = self.original_schema_resolver
        legacy_router._load_checkpoint = self.original_load
        legacy_router._save_checkpoint = self.original_save

    def test_rejects_prompt_inferred_schema_authority(self) -> None:
        legacy_router._structured_schema_for_prompt = lambda prompt: ("legacy", {})
        with mock.patch.object(stage_contract, "assert_planning_stage_contract_installed"):
            with self.assertRaises(stage_contract.PlanningStageError) as raised:
                guard.install_legacy_planning_authority_guard()
        self.assertEqual(
            raised.exception.code,
            stage_contract.PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
        )
        self.assertIn("prompt-inferred", raised.exception.detail)

    def test_seal_blocks_legacy_checkpoint_read_and_write(self) -> None:
        legacy_router._structured_schema_for_prompt = stage_contract._explicit_schema_adapter
        with mock.patch.object(stage_contract, "assert_planning_stage_contract_installed"):
            guard.install_legacy_planning_authority_guard()
            guard.assert_legacy_planning_authority_sealed()

        for call in (
            lambda: legacy_router._load_checkpoint(),
            lambda: legacy_router._save_checkpoint({"version": 1, "responses": {}}),
        ):
            with self.assertRaises(stage_contract.PlanningStageError) as raised:
                call()
            self.assertEqual(
                raised.exception.code,
                stage_contract.PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
            )
            self.assertIn("legacy Planning checkpoint authority is disabled", raised.exception.detail)


if __name__ == "__main__":
    unittest.main()

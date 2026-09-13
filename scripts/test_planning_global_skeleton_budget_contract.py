from __future__ import annotations

import json
import unittest

from isco_video_agent import adaptive_outline_contract as engine_contract

from scripts import planning_global_skeleton_budget_contract as budget_contract
from scripts import planning_outline_adaptive_sharding as adaptive
from scripts import planning_stage_contract as stage_contract


class GlobalSkeletonDerivedBudgetContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._prompt = engine_contract.core_prompt_with_global_skeleton
        self._validate_core = adaptive._validate_core
        self._core_stage_spec = adaptive._core_stage_spec

    def tearDown(self) -> None:
        engine_contract.core_prompt_with_global_skeleton = self._prompt
        adaptive._validate_core = self._validate_core
        adaptive._core_stage_spec = self._core_stage_spec

    @staticmethod
    def _contract(count: int = 8):
        spec = adaptive._core_stage_spec(count)
        return stage_contract.bind_request_contract(
            spec,
            f"test-global-skeleton-derived-budget:{count}",
        )

    @staticmethod
    def _skeleton(
        *,
        count: int = 8,
        id_text: str = "sec",
        purpose_text: str = "وظيفة تحريرية موجزة",
    ) -> list[dict]:
        return [
            {
                "id": f"{id_text}{index}",
                "purpose": purpose_text,
                "arc_position": index,
            }
            for index in range(1, count + 1)
        ]

    def test_film_budget_is_derived_from_aggregate_and_structural_overhead(self) -> None:
        budget = budget_contract.derive_skeleton_field_budget(8)
        self.assertEqual(
            budget["aggregate_limit_bytes"],
            engine_contract.GLOBAL_SECTION_SKELETON_MAX_UTF8_BYTES,
        )
        self.assertEqual(budget["structural_bytes"], 321)
        self.assertEqual(budget["structural_share_bytes"], 41)
        self.assertEqual(budget["id_hard_bytes"], 41)
        self.assertEqual(budget["aggregate_reserve_bytes"], 41)
        self.assertEqual(budget["purpose_soft_bytes"], 138)
        self.assertEqual(budget["purpose_hard_bytes"], 179)

        # The numbers must remain mathematically bound to the aggregate contract rather
        # than becoming another independent magic-number ceiling.
        funded = (
            budget["structural_bytes"]
            + (8 * budget["id_hard_bytes"])
            + (8 * budget["purpose_soft_bytes"])
            + budget["aggregate_reserve_bytes"]
        )
        self.assertLessEqual(funded, budget["aggregate_limit_bytes"])

    def test_story_budget_rederives_from_section_count(self) -> None:
        film = budget_contract.derive_skeleton_field_budget(8)
        story = budget_contract.derive_skeleton_field_budget(5)
        self.assertNotEqual(story["purpose_soft_bytes"], film["purpose_soft_bytes"])
        self.assertGreater(story["purpose_soft_bytes"], film["purpose_soft_bytes"])
        self.assertEqual(story["aggregate_limit_bytes"], film["aggregate_limit_bytes"])

    def test_prompt_exposes_utf8_budget_and_editorial_function_shape(self) -> None:
        budget_contract.install_global_skeleton_derived_budget_contract()
        prompt = engine_contract.core_prompt_with_global_skeleton("CORE", 8)
        budget = budget_contract.derive_skeleton_field_budget(8)

        self.assertEqual(prompt.count("<GLOBAL_SKELETON_DERIVED_BUDGET_V1>"), 1)
        self.assertIn("ONE concise editorial function only", prompt)
        self.assertIn("no explanation, examples, reasoning, or visual direction", prompt)
        self.assertIn(f"target <= {budget['purpose_soft_bytes']} UTF-8 bytes", prompt)
        self.assertIn(f"hard <= {budget['purpose_hard_bytes']} UTF-8 bytes", prompt)
        self.assertIn(
            f"whole-skeleton hard limit is {budget['aggregate_limit_bytes']} UTF-8 bytes",
            prompt,
        )

    def test_single_oversized_arabic_purpose_is_structural_not_capacity(self) -> None:
        budget_contract.install_global_skeleton_derived_budget_contract()
        budget = budget_contract.derive_skeleton_field_budget(8)
        oversized = "ا" * ((budget["purpose_hard_bytes"] // 2) + 1)
        skeleton = self._skeleton(purpose_text=oversized)
        before = json.loads(json.dumps(skeleton, ensure_ascii=False))

        with self.assertRaises(stage_contract.PlanningStageError) as ctx:
            budget_contract._validate_budgeted_skeleton(skeleton, self._contract())

        self.assertEqual(ctx.exception.code, stage_contract.PlanningErrorCode.STRUCTURAL_INVALID)
        self.assertIn("global_skeleton_purpose_portability_budget_exceeded", str(ctx.exception))
        self.assertNotIn("CAPACITY", str(ctx.exception))
        self.assertEqual(skeleton, before, "validator must never truncate or rewrite Arabic")

    def test_aggregate_1849_family_is_classified_before_engine_semantic_mapping(self) -> None:
        budget_contract.install_global_skeleton_derived_budget_contract()
        budget = budget_contract.derive_skeleton_field_budget(8)

        # Keep every individual field under its hard ceiling while making the compact
        # aggregate exceed 1800. This reproduces the failure family rather than relying
        # only on one oversized purpose.
        ids = []
        skeleton = []
        for index in range(1, 9):
            suffix = str(index)
            section_id = ("x" * (budget["id_hard_bytes"] - len(suffix))) + suffix
            ids.append(section_id)
            skeleton.append(
                {
                    "id": section_id,
                    "purpose": "y" * min(160, budget["purpose_hard_bytes"]),
                    "arc_position": index,
                }
            )
        compact_bytes = len(
            json.dumps(skeleton, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        self.assertGreater(compact_bytes, budget["aggregate_limit_bytes"])

        with self.assertRaises(stage_contract.PlanningStageError) as ctx:
            budget_contract._validate_budgeted_skeleton(skeleton, self._contract())

        self.assertEqual(ctx.exception.code, stage_contract.PlanningErrorCode.STRUCTURAL_INVALID)
        self.assertIn("global_section_skeleton_portability_budget_exceeded", str(ctx.exception))
        self.assertIn("limit=1800", str(ctx.exception))

    def test_core_spec_carries_budget_trace_without_changing_provider_policy(self) -> None:
        before = adaptive._core_stage_spec(8)
        budget_contract.install_global_skeleton_derived_budget_contract()
        after = adaptive._core_stage_spec(8)
        budget = budget_contract.derive_skeleton_field_budget(8)

        self.assertEqual(after.provider_policy, before.provider_policy)
        self.assertEqual(after.output_schema, before.output_schema)
        self.assertEqual(
            after.semantic_rules["global_skeleton_purpose_soft_utf8_bytes"],
            budget["purpose_soft_bytes"],
        )
        self.assertEqual(
            after.semantic_rules["global_skeleton_purpose_hard_utf8_bytes"],
            budget["purpose_hard_bytes"],
        )
        self.assertEqual(
            after.semantic_rules["global_skeleton_id_hard_utf8_bytes"],
            budget["id_hard_bytes"],
        )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest

from isco_video_agent import adaptive_outline_contract as engine_contract

from scripts import planning_outline_adaptive_sharding as adaptive
from scripts import planning_stage_contract as stage_contract


def _skeleton() -> list[dict]:
    return [
        {"id": f"s{i}", "purpose": f"غرض القسم {i}", "arc_position": i}
        for i in range(1, 9)
    ]


def _payload_for_active_spec(skeleton: list[dict]) -> dict:
    spec = stage_contract._ACTIVE_STAGE_SPEC.get()
    assert spec is not None
    ids = list(spec.semantic_rules["expected_ids"])
    by_id = {item["id"]: item for item in skeleton}
    return {
        "section_briefs": [
            {
                "id": section_id,
                "purpose": by_id[section_id]["purpose"],
                "visual_query": "quiet room",
                "on_screen_text": "نص",
                "emotion": "calm",
                "expected_seconds": 30,
            }
            for section_id in ids
        ]
    }


class AdaptiveOutlineShardingTests(unittest.TestCase):
    def _state(self) -> adaptive._AdaptiveOutlineState:
        state = adaptive._AdaptiveOutlineState(fmt="film", expected_count=8)
        state.phase = "sections_inflight"
        state.skeleton = _skeleton()
        return state

    def test_hard_budgets_are_derived_and_bounded(self) -> None:
        state = self._state()
        self.assertEqual(state.max_section_requests, 15)
        self.assertEqual(state.provider_attempt_limit, 30)
        self.assertEqual(adaptive.OUTLINE_PROVIDER_ATTEMPT_HARD_MAX, 30)
        self.assertEqual(adaptive.SHARD_MAX_TOTAL_ATTEMPTS, 3)
        self.assertEqual(adaptive._section_completion_tokens(8), 1800)
        self.assertEqual(adaptive._section_completion_tokens(4), 1200)
        self.assertEqual(adaptive._section_completion_tokens(2), 800)
        self.assertEqual(adaptive._section_completion_tokens(1), 600)

    def test_core_schema_requires_global_skeleton_without_section_briefs(self) -> None:
        schema = adaptive.outline_core_schema(8)
        self.assertIn(engine_contract.GLOBAL_SECTION_SKELETON_FIELD, schema["required"])
        self.assertNotIn("section_briefs", schema["properties"])
        skeleton = schema["properties"][engine_contract.GLOBAL_SECTION_SKELETON_FIELD]
        self.assertEqual(skeleton["minItems"], 8)
        self.assertEqual(skeleton["maxItems"], 8)
        self.assertFalse(skeleton["items"]["additionalProperties"])

    def test_sharding_allow_list_rejects_run579_window_and_transients(self) -> None:
        self.assertFalse(
            adaptive.is_shardable_sections_failure(
                stage_contract.PlanningStageError(
                    stage_contract.PlanningErrorCode.CAPACITY,
                    "GROQ_TPM_WINDOW_BUSY_PRECHECK tpm_capacity_preflight reset_in=30s",
                )
            )
        )
        self.assertFalse(
            adaptive.is_shardable_sections_failure(
                stage_contract.PlanningStageError(
                    stage_contract.PlanningErrorCode.PROVIDER_TRANSIENT,
                    "network timeout after 30 seconds",
                )
            )
        )
        # Mixed evidence is intentionally conservative: a transient/window veto wins.
        self.assertFalse(
            adaptive.is_shardable_sections_failure(
                stage_contract.PlanningStageError(
                    stage_contract.PlanningErrorCode.CAPACITY,
                    "structured_generation_failed | tpm_window reset_in=20s",
                )
            )
        )

    def test_sharding_allow_list_accepts_only_proven_size_or_generation_exhaustion(self) -> None:
        for detail in (
            "GEMINI_INTERACTION_OUTPUT_TRUNCATED incomplete_max_tokens",
            "structured_generation_failed code=json_validate_failed",
            "payload_too_large for this exact request",
            "context_length max_tokens",
            "GROQ_ACTUAL_TPM_BELOW_REQUEST",
        ):
            with self.subTest(detail=detail):
                self.assertTrue(
                    adaptive.is_shardable_sections_failure(
                        stage_contract.PlanningStageError(
                            stage_contract.PlanningErrorCode.CAPACITY,
                            detail,
                        )
                    )
                )
        self.assertFalse(
            adaptive.is_shardable_sections_failure(
                stage_contract.PlanningStageError(
                    stage_contract.PlanningErrorCode.SEMANTIC_INVALID,
                    "duplicate purpose",
                )
            )
        )

    def test_fast_path_is_one_sections_request_after_core(self) -> None:
        state = self._state()
        skeleton = state.skeleton

        def fake_json(_api_key, _prompt, model="model"):
            return _payload_for_active_spec(skeleton)

        all_ids = tuple(item["id"] for item in skeleton)
        briefs = adaptive._request_section_batch(
            fake_json,
            api_key="k",
            model="m",
            base_prompt="BASE",
            state=state,
            requested_ids=all_ids,
            root=True,
        )
        self.assertEqual([item["id"] for item in briefs], list(all_ids))
        self.assertEqual(state.request_trace, [all_ids])
        self.assertEqual(state.section_request_count, 1)

    def test_root_8_failure_splits_to_4_plus_4_only(self) -> None:
        state = self._state()
        skeleton = state.skeleton
        calls = 0

        def fake_json(_api_key, _prompt, model="model"):
            nonlocal calls
            calls += 1
            spec = stage_contract._ACTIVE_STAGE_SPEC.get()
            ids = tuple(spec.semantic_rules["expected_ids"])
            if len(ids) == 8:
                raise stage_contract.PlanningStageError(
                    stage_contract.PlanningErrorCode.CAPACITY,
                    "structured_generation_failed",
                )
            return _payload_for_active_spec(skeleton)

        all_ids = tuple(item["id"] for item in skeleton)
        briefs = adaptive._request_section_batch(
            fake_json,
            api_key="k",
            model="m",
            base_prompt="BASE",
            state=state,
            requested_ids=all_ids,
            root=True,
        )
        self.assertEqual(calls, 3)
        self.assertEqual(
            state.request_trace,
            [all_ids, all_ids[:4], all_ids[4:]],
        )
        self.assertEqual([item["id"] for item in briefs], list(all_ids))

    def test_only_failed_subtree_descends_4_to_2_to_1(self) -> None:
        state = self._state()
        skeleton = state.skeleton

        def fake_json(_api_key, _prompt, model="model"):
            spec = stage_contract._ACTIVE_STAGE_SPEC.get()
            ids = tuple(spec.semantic_rules["expected_ids"])
            if ids in {
                tuple(f"s{i}" for i in range(1, 9)),
                ("s5", "s6", "s7", "s8"),
                ("s7", "s8"),
            }:
                raise stage_contract.PlanningStageError(
                    stage_contract.PlanningErrorCode.CAPACITY,
                    "structured_generation_failed",
                )
            return _payload_for_active_spec(skeleton)

        all_ids = tuple(item["id"] for item in skeleton)
        briefs = adaptive._request_section_batch(
            fake_json,
            api_key="k",
            model="m",
            base_prompt="BASE",
            state=state,
            requested_ids=all_ids,
            root=True,
        )
        self.assertEqual(
            state.request_trace,
            [
                all_ids,
                ("s1", "s2", "s3", "s4"),
                ("s5", "s6", "s7", "s8"),
                ("s5", "s6"),
                ("s7", "s8"),
                ("s7",),
                ("s8",),
            ],
        )
        self.assertNotIn(("s1", "s2"), state.request_trace)
        self.assertEqual([item["id"] for item in briefs], list(all_ids))

    def test_window_failure_never_enters_recursive_sharding(self) -> None:
        state = self._state()
        skeleton = state.skeleton

        def fake_json(_api_key, _prompt, model="model"):
            raise stage_contract.PlanningStageError(
                stage_contract.PlanningErrorCode.CAPACITY,
                "GROQ_TPM_WINDOW_BUSY_PRECHECK tpm_window",
            )

        all_ids = tuple(item["id"] for item in skeleton)
        with self.assertRaises(stage_contract.PlanningStageError):
            adaptive._request_section_batch(
                fake_json,
                api_key="k",
                model="m",
                base_prompt="BASE",
                state=state,
                requested_ids=all_ids,
                root=True,
            )
        self.assertEqual(state.request_trace, [all_ids])

    def test_shard_policy_disables_second_sweep_but_root_preserves_it(self) -> None:
        skeleton = _skeleton()
        root = adaptive._sections_stage_spec(
            skeleton, tuple(item["id"] for item in skeleton), root=True
        )
        shard = adaptive._sections_stage_spec(
            skeleton, ("s1", "s2", "s3", "s4"), root=False
        )
        self.assertTrue(root.provider_policy.second_pass_after_full_exhaustion)
        self.assertEqual(
            root.provider_policy.max_total_attempts,
            stage_contract.OUTLINE_MAX_TOTAL_ATTEMPTS,
        )
        self.assertFalse(shard.provider_policy.second_pass_after_full_exhaustion)
        self.assertEqual(shard.provider_policy.max_total_attempts, 3)


if __name__ == "__main__":
    unittest.main()

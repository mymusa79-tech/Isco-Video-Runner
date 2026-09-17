from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from scripts import append_retry_guard as guard
from scripts import planning_stage_contract as stage_contract
from scripts import provider_capacity_hardening as capacity
from scripts import task_level_planner_router as router


class AppendWorkloadBudget268Tests(unittest.TestCase):
    @staticmethod
    def _words(count: int) -> str:
        return " ".join(["كلمة"] * count)

    def _capture_guard_spec(self, counts: list[int]):
        sections = [
            SimpleNamespace(
                id=f"sec_{index}",
                narration=self._words(words),
                key_point=f"key-{index}",
            )
            for index, words in enumerate(counts, start=1)
        ]
        captured = []

        def fake_json_text(_api_key, _prompt, model=None):
            spec = stage_contract._ACTIVE_STAGE_SPEC.get()
            self.assertIsNotNone(spec)
            captured.append(spec)
            additions = []
            for section, words in zip(sections, counts):
                deficit = 110 - words
                # The guard adds 30 preferred safety words in every case exercised
                # here because aggregate headroom is ample. Returning that exact
                # preferred minimum keeps the response inside all existing hard bands.
                additions.append(
                    {
                        "id": section.id,
                        "append_text": self._words(deficit + 30),
                    }
                )
            return {"additions": additions}

        narrative_format = next(iter(guard.staged._NARRATIVE_FORMATS))
        with patch.object(guard.staged, "json_text", side_effect=fake_json_text):
            additions = guard._repair_all_residual_underlength(
                "unused",
                topic="موضوع",
                model="unused-model",
                sections=sections,
                policy_json="{}",
                research_json="{}",
                narrative_format=narrative_format,
                current_words=sum(counts),
                minimum=800,
            )
        self.assertEqual(list(additions), [section.id for section in sections])
        self.assertEqual(len(captured), 1)
        return captured[0], sections

    def test_run268_guard_passes_exact_workload_budget_and_capacity(self) -> None:
        counts = [95, 94, 94, 94, 94, 94, 94, 95]
        spec, _sections = self._capture_guard_spec(counts)
        rules = spec.semantic_rules

        self.assertEqual(sum(counts), 754)
        self.assertEqual(rules["append_target_count"], 8)
        self.assertEqual(rules["append_required_floor_words"], 126)
        self.assertEqual(rules["append_minimum_words"], 366)
        self.assertEqual(rules["append_maximum_words"], 510)
        self.assertEqual(rules["append_budget_basis"], "workload")
        self.assertEqual(rules["append_completion_budget"], 1833)
        self.assertEqual(spec.provider_policy.completion_tokens_for("groq"), 1833)

        # Same observed incident geometry as the historical baseline, used only to
        # prove the before/after admission consequence. It is not an estimator input.
        estimate = capacity.groq_capacity_estimate(
            "x" * 21_538,
            model_name="openai/gpt-oss-120b",
            reserved_completion_tokens=spec.provider_policy.completion_tokens,
            contract_name=spec.contract_id,
        )
        effective_tpm_limit = int(8_000 * 0.90)
        self.assertEqual(estimate["estimated_request_tokens"], 7_151)
        self.assertEqual(effective_tpm_limit, 7_200)
        self.assertLessEqual(estimate["estimated_request_tokens"], effective_tpm_limit)

    def test_small_medium_heavy_and_worst_valid_guard_envelopes(self) -> None:
        cases = (
            ([100], 10, 40, 58, 253),
            ([100, 100, 100, 100], 40, 160, 232, 871),
            ([80] * 8, 240, 480, 624, 2175),
            ([0] * 8, 880, 1120, 1264, 4095),
        )
        for counts, required, minimum, maximum, budget in cases:
            with self.subTest(targets=len(counts), counts=counts[:1]):
                spec, _sections = self._capture_guard_spec(counts)
                rules = spec.semantic_rules
                self.assertEqual(rules["append_required_floor_words"], required)
                self.assertEqual(rules["append_minimum_words"], minimum)
                self.assertEqual(rules["append_maximum_words"], maximum)
                self.assertEqual(rules["append_completion_budget"], budget)
                self.assertEqual(rules["append_budget_basis"], "workload")

    def test_worst_valid_max_output_is_complete_and_inside_existing_bounds(self) -> None:
        spec, sections = self._capture_guard_spec([0] * 8)
        maximums = [158] * 8
        additions = {
            section.id: self._words(words)
            for section, words in zip(sections, maximums)
        }
        target_specs = [
            {
                "id": section.id,
                "current_words": 0,
                "hard_section_band": [110, 170],
                "minimum_append_words": 140,
                "maximum_append_words": 158,
            }
            for section in sections
        ]
        guard._validate_addition_bounds(
            additions,
            target_specs,
            aggregate_headroom=1450,
        )
        payload = {
            "additions": [
                {"id": section.id, "append_text": additions[section.id]}
                for section in sections
            ]
        }
        bound = stage_contract.bind_request_contract(spec, "worst-valid")
        self.assertEqual(stage_contract.validate_response(bound, payload), payload)
        self.assertEqual(sum(maximums), 1264)
        self.assertEqual(spec.provider_policy.completion_tokens, 4095)

    def test_contract_max_fallback_is_not_the_historical_target_count_ladder(self) -> None:
        ids = [f"sec_{index}" for index in range(1, 9)]
        spec = stage_contract.append_stage_spec(ids)
        self.assertEqual(spec.semantic_rules["append_budget_basis"], "contract_max")
        self.assertEqual(spec.semantic_rules["append_maximum_words"], 1360)
        self.assertEqual(spec.provider_policy.completion_tokens, 4383)
        self.assertNotEqual(
            spec.provider_policy.completion_tokens,
            stage_contract.SHARD_COMPLETION_TOKEN_BUDGETS["append_repair_8"],
        )

    def test_partial_or_unbounded_workload_fails_closed(self) -> None:
        with self.assertRaises(stage_contract.PlanningStageError):
            stage_contract.append_stage_spec(
                ["sec_1"],
                required_floor_words=10,
            )
        with self.assertRaises(stage_contract.PlanningStageError):
            stage_contract.append_stage_spec(
                ["sec_1"],
                required_floor_words=10,
                minimum_append_words=40,
                maximum_append_words=171,
            )

    def test_routing_retry_and_admission_policy_are_unchanged(self) -> None:
        spec = stage_contract.append_stage_spec(
            ["sec_1"],
            required_floor_words=10,
            minimum_append_words=40,
            maximum_append_words=58,
        )
        self.assertEqual(spec.provider_policy.providers, stage_contract._planning_provider_order())
        self.assertEqual(
            spec.provider_policy.max_attempts_per_provider,
            router.TRANSIENT_PROVIDER_MAX_ATTEMPTS,
        )
        self.assertEqual(
            spec.provider_policy.max_total_attempts,
            len(spec.provider_policy.providers) * router.TRANSIENT_PROVIDER_MAX_ATTEMPTS,
        )
        self.assertEqual(
            spec.provider_policy.max_prompt_utf8_bytes,
            (("groq", router.GROQ_MAX_PROMPT_UTF8_BYTES),),
        )
        self.assertFalse(spec.provider_policy.second_pass_after_full_exhaustion)

    def test_append_workload_metadata_is_exposed_without_new_telemetry_owner(self) -> None:
        spec = stage_contract.append_stage_spec(
            ["sec_1"],
            required_floor_words=10,
            minimum_append_words=40,
            maximum_append_words=58,
        )
        bound = stage_contract.bind_request_contract(spec, "prompt")
        metadata = stage_contract._admission_metadata(bound, "prompt")
        self.assertEqual(metadata["append_target_count"], 1)
        self.assertEqual(metadata["append_required_floor_words"], 10)
        self.assertEqual(metadata["append_minimum_words"], 40)
        self.assertEqual(metadata["append_maximum_words"], 58)
        self.assertEqual(metadata["append_completion_budget"], 253)
        self.assertEqual(metadata["append_budget_basis"], "workload")
        self.assertEqual(metadata["planned_completion_tokens"], 253)
        expected_capacity = capacity.groq_capacity_estimate(
            "prompt",
            reserved_completion_tokens=253,
            contract_name=bound.contract_id,
        )
        self.assertEqual(
            metadata["estimated_request_tokens"],
            expected_capacity["estimated_request_tokens"],
        )

    def test_budget_is_deterministic_and_independent_of_provider_limits(self) -> None:
        kwargs = dict(
            required_floor_words=126,
            minimum_append_words=366,
            maximum_append_words=510,
        )
        ids = [f"sec_{index}" for index in range(1, 9)]
        first = stage_contract.append_stage_spec(ids, **kwargs)
        second = stage_contract.append_stage_spec(ids, **kwargs)
        self.assertEqual(first.provider_policy.completion_tokens, 1833)
        self.assertEqual(
            first.provider_policy.completion_tokens,
            second.provider_policy.completion_tokens,
        )
        serialized = json.dumps(first.semantic_rules, sort_keys=True, ensure_ascii=False)
        self.assertNotIn("7200", serialized)
        self.assertNotIn("8000", serialized)


if __name__ == "__main__":
    unittest.main()

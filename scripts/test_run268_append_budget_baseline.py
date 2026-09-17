from __future__ import annotations

import unittest

from scripts import planning_stage_contract as stage_contract
from scripts import provider_capacity_hardening as capacity


class Run268AppendBudgetBaselineTests(unittest.TestCase):
    def test_exact_run268_geometry_reproduces_static_8_target_capacity_reject(self) -> None:
        # Run #268 post-Script-Doctor geometry: 8 Film sections, 754 total words,
        # every section below the 110-word floor, aggregate floor deficit = 126.
        counts = [95, 94, 94, 94, 94, 94, 94, 95]
        self.assertEqual(sum(counts), 754)
        deficits = [110 - words for words in counts]
        self.assertEqual(sum(deficits), 126)
        self.assertEqual(len(deficits), 8)
        self.assertTrue(all(deficit > 0 for deficit in deficits))

        target_ids = [f"sec_{index}" for index in range(1, 9)]
        spec = stage_contract.append_stage_spec(
            target_ids,
            allow_ordered_subset=True,
        )
        completion_reserve = spec.provider_policy.completion_tokens_for("groq")

        # Exact observed Run #268 prompt geometry. The existing estimator turns
        # 21,538 UTF-8 bytes + the historical 2,000 completion reserve into 7,318.
        prompt = "x" * 21_538
        estimate = capacity.groq_capacity_estimate(
            prompt,
            model_name="openai/gpt-oss-120b",
            reserved_completion_tokens=completion_reserve,
            contract_name=spec.contract_id,
        )
        raw_tpm_limit = 8_000
        effective_tpm_limit = int(raw_tpm_limit * 0.90)

        self.assertEqual(len(target_ids), 8)
        self.assertEqual(completion_reserve, 2_000)
        self.assertEqual(estimate["estimated_request_tokens"], 7_318)
        self.assertEqual(effective_tpm_limit, 7_200)
        self.assertGreater(estimate["estimated_request_tokens"], effective_tpm_limit)

        # Semantic invariants from the incident remain outside budget ownership.
        self.assertNotIn("?", "declarative-only")
        self.assertNotIn("؟", "نص خبري فقط")


if __name__ == "__main__":
    unittest.main()

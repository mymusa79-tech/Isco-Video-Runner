from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts import dynamic_planning_capacity as dynamic
from scripts import planning_stage_contract as contract
from scripts import provider_capacity_hardening as capacity


class Run140PlanningRedundancyTests(unittest.TestCase):
    def setUp(self) -> None:
        capacity.reset_groq_capacity_state_for_tests()
        self.tmp = tempfile.TemporaryDirectory()
        self.preflight = Path(self.tmp.name) / "provider-preflight.json"
        self.preflight.write_text(
            json.dumps(
                {
                    "checks": [
                        {"provider": "gemini", "status": "pass"},
                        {"provider": "groq", "status": "pass"},
                        {"provider": "openrouter", "status": "block"},
                    ]
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        capacity.reset_groq_capacity_state_for_tests()
        self.tmp.cleanup()

    def test_outline_contract_restores_groq_portability_and_fast_failover(self) -> None:
        spec = contract.outline_stage_spec(6)
        self.assertEqual(spec.provider_policy.completion_tokens, 2400)
        self.assertEqual(spec.provider_policy.max_attempts_per_provider, 1)
        # Run #142: budget for exactly two full one-attempt-per-provider sweeps, not one -
        # see test_run142_outline_second_pass_retry.py for the second-pass contract itself.
        self.assertEqual(spec.provider_policy.max_total_attempts, 6)
        self.assertTrue(spec.provider_policy.second_pass_after_full_exhaustion)
        self.assertEqual(spec.provider_policy.providers, ("gemini", "groq", "openrouter"))

    def test_exact_run140_envelope_is_below_initial_8k_with_restored_budget(self) -> None:
        # Run #140 telemetry: 20,635 UTF-8 prompt bytes. Using an ASCII string keeps the
        # byte count exact while exercising the same conservative byte/token estimator.
        prompt = "x" * 20635
        estimate = capacity.groq_capacity_estimate(
            prompt,
            reserved_completion_tokens=contract.OUTLINE_COMPLETION_TOKEN_BUDGET,
            contract_name="editorial_outline",
        )
        self.assertEqual(estimate["estimated_prompt_tokens"], 4856)
        self.assertEqual(estimate["reserved_completion_tokens"], 2400)
        self.assertEqual(estimate["estimated_request_tokens"], 7506)
        self.assertEqual(estimate["provider_tpm_limit"], 8000)
        self.assertGreater(8000 - estimate["estimated_request_tokens"], 0)

    def test_p0_gate_accepts_gemini_plus_groq_when_openrouter_is_blocked(self) -> None:
        viable = dynamic.require_viable_planning_capacity(
            7506,
            phase="run140_regression",
            preflight_path=self.preflight,
            min_provider_families=2,
        )
        self.assertEqual(dynamic.viable_provider_families(viable), ["gemini", "groq"])

    def test_old_run140_8306_envelope_fails_two_family_gate_before_network(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "PLANNING_CAPACITY_REDUNDANCY_REQUIRED"):
            dynamic.require_viable_planning_capacity(
                8306,
                phase="run140_regression",
                preflight_path=self.preflight,
                min_provider_families=2,
            )

    def test_multiple_models_do_not_fake_independent_provider_redundancy(self) -> None:
        self.assertEqual(
            dynamic.viable_provider_families(
                ["groq:openai/gpt-oss-20b", "groq:openai/gpt-oss-120b"]
            ),
            ["groq"],
        )


if __name__ == "__main__":
    unittest.main()

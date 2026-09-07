from __future__ import annotations

import unittest

import isco_video_agent.production_pipeline as production_pipeline
from isco_video_agent.ai_budget import Capability, Priority

from scripts import run123_budget_closure as closure


class FinalCriticP0TaskPrefixContractTests(unittest.TestCase):
    def test_gold_keyword_call_is_supported_and_forwarded(self) -> None:
        original = production_pipeline._final_critic_spec

        with closure.enforcing_final_critic_as_p0():
            spec = production_pipeline._final_critic_spec(
                "FINAL_CRITIC_RELEASE_REVIEW",
                Capability.TEXT,
                task_prefix="GOLD_",
                task_kind="GOLD_FINAL_CRITIC",
            )
            self.assertEqual(spec.task_id, "GOLD_FINAL_CRITIC_RELEASE_REVIEW")
            self.assertEqual(spec.kind, "GOLD_FINAL_CRITIC")
            self.assertIs(spec.priority, Priority.P0)

        self.assertIs(production_pipeline._final_critic_spec, original)

    def test_bootstrap_probe_reproduces_exact_gold_call_shape_without_provider_work(self) -> None:
        original = production_pipeline._final_critic_spec
        report = closure.certify_enforcing_final_critic_p0_contract()

        self.assertEqual(
            report,
            {
                "status": "pass",
                "task_prefix_forwarded": True,
                "task_kind_forwarded": True,
                "priority": "P0",
                "provider_calls": 0,
            },
        )
        self.assertIs(production_pipeline._final_critic_spec, original)


if __name__ == "__main__":
    unittest.main()

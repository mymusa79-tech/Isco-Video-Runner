from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import isco_video_agent.resilient_planner as staged

from scripts import checkpoint_namespace_guard as checkpoint_guard
from scripts import planning_outline_split_contract as split
from scripts import planning_stage_contract as contract
from scripts import task_level_planner_router as router
from scripts.planning_split_retry_policy import install_planning_split_retry_policy


def _core_payload() -> dict:
    return {
        "pillar": "understand",
        "hook": "خطاف",
        "title_options": ["أ", "ب", "ج"],
        "thumbnail_concepts": ["a", "b", "c"],
        "cta": "ختام",
        "closing_payoff": "خلاصة",
        "narrative_format": "direct_cinematic",
        "opener_variant": "افتتاح",
        "closer_variant": "إغلاق",
        "transition_variants": ["أول", "ثان", "ثالث"],
        "editorial_intent": {
            "editorial_thesis": "فكرة",
            "viewer_starting_belief": "اعتقاد",
            "hidden_assumption": "افتراض",
            "editorial_turn": "تحول",
            "stakes": "رهان",
            "viewer_promise": "وعد",
            "evidence_boundaries": ["حد"],
            "earned_payoff": "عائد",
        },
    }


class PlanningSplitRetryRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self.tmp.name) / "checkpoint.json"
        self.old_json_text = staged.json_text
        self.old_policy = split._split_provider_policy
        self.cache_patch = mock.patch.object(router, "CACHE_PATH", self.cache)
        self.sleep_patch = mock.patch.object(contract.time, "sleep")
        self.router_sleep_patch = mock.patch.object(router.time, "sleep")
        self.cache_patch.start()
        self.sleep_patch.start()
        self.router_sleep_patch.start()
        staged.json_text = lambda *_args, **_kwargs: {}
        router._USED_PROVIDERS.clear()
        router._TELEMETRY.clear()
        checkpoint_guard.install_checkpoint_namespace_guard()
        install_planning_split_retry_policy()
        contract.install_planning_contract_router()

    def tearDown(self) -> None:
        staged.json_text = self.old_json_text
        split._split_provider_policy = self.old_policy
        self.router_sleep_patch.stop()
        self.sleep_patch.stop()
        self.cache_patch.stop()
        self.tmp.cleanup()

    def test_transient_gemini_gets_second_attempt_before_capacity_fallback_is_needed(self) -> None:
        payload = _core_payload()
        calls = {"gemini": 0, "groq": 0}

        def gemini(*_args, **_kwargs):
            calls["gemini"] += 1
            if calls["gemini"] == 1:
                raise TimeoutError("client-side timeout")
            return payload

        def groq(_prompt):
            calls["groq"] += 1
            raise RuntimeError("GROQ_TPM_WINDOW_BUSY_PRECHECK tpm_capacity")

        spec = split.outline_core_stage_spec_for_format("film")
        with mock.patch.object(router, "gemini_json_text", side_effect=gemini), \
                mock.patch.object(router, "_groq_call", side_effect=groq), \
                contract.request_stage_scope(spec):
            result = staged.json_text("request-key", "opaque")

        self.assertEqual(result, payload)
        self.assertEqual(calls["gemini"], 2)
        self.assertEqual(calls["groq"], 0)

    def test_capacity_failure_is_not_retried_for_same_provider(self) -> None:
        calls = {"groq": 0}
        base = split.outline_core_stage_spec_for_format("film")
        policy = contract.ProviderPolicy(
            providers=("groq",),
            max_attempts_per_provider=2,
            max_total_attempts=2,
            completion_tokens=base.provider_policy.completion_tokens,
            max_prompt_utf8_bytes=base.provider_policy.max_prompt_utf8_bytes,
        )
        spec = contract.PlanningStageSpec(
            stage_id=base.stage_id,
            contract_id=base.contract_id,
            output_schema=base.output_schema,
            semantic_rules=base.semantic_rules,
            provider_policy=policy,
            cache_policy=base.cache_policy,
        )

        def groq(_prompt):
            calls["groq"] += 1
            raise RuntimeError("GROQ_TPM_WINDOW_BUSY_PRECHECK tpm_capacity")

        with mock.patch.object(router, "_groq_call", side_effect=groq), \
                contract.request_stage_scope(spec):
            with self.assertRaises(contract.PlanningStageError) as captured:
                staged.json_text("request-key", "opaque")

        self.assertEqual(captured.exception.code, contract.PlanningErrorCode.CAPACITY)
        self.assertEqual(calls["groq"], 1)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import dataclasses
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import isco_video_agent.resilient_planner as staged

from scripts import planning_stage_contract as contract
from scripts import task_level_planner_router as router


_GEMINI_OUTPUT_TRUNCATED = "GEMINI_INTERACTION_OUTPUT_TRUNCATED"
_GROQ_JSON_VALIDATE_FAILED = "GROQ_JSON_VALIDATE_FAILED status=400 code=json_validate_failed"
_OPENROUTER_SPEND_BLOCKED = (
    "OPENROUTER_UNAVAILABLE_THIS_RUN reason=preflight_blocked: "
    "openrouter readiness blocked: key spend capacity exhausted"
)


def _script(ids: list[str]) -> dict:
    return {
        "sections": [
            {
                "id": section_id,
                "narration": f"narration {section_id}",
                "key_point": f"key-{section_id}",
            }
            for section_id in ids
        ]
    }


class PlanningOutputTruncationFailoverTests(unittest.TestCase):
    """Regression for provider-first failover plus selective transient retry."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cache_path = Path(self.tmp.name) / "planning-checkpoint.json"
        self.key_path = Path(self.tmp.name) / "gemini-key"
        self.key_path.write_text("fake-key", encoding="utf-8")
        self.env = patch.dict(
            os.environ,
            {"GEMINI_API_KEY_FILE": str(self.key_path)},
            clear=False,
        )
        self.env.start()
        self.cache = patch.object(router, "CACHE_PATH", self.cache_path)
        self.cache.start()

        self._clock = 1_000.0

        def _fake_monotonic() -> float:
            return self._clock

        def _fake_sleep(seconds: float) -> None:
            self._clock += seconds

        self.time_monotonic = patch.object(
            contract.time,
            "monotonic",
            side_effect=_fake_monotonic,
        )
        self.time_monotonic.start()
        self.time_sleep = patch.object(contract.time, "sleep", side_effect=_fake_sleep)
        self.contract_sleep_mock = self.time_sleep.start()

        self.old_json_text = staged.json_text
        self.old_schema_adapter = router._structured_schema_for_prompt
        staged.json_text = lambda *_args, **_kwargs: {}
        router._USED_PROVIDERS.clear()
        router._TELEMETRY.clear()
        contract.install_planning_contract_router()

    def tearDown(self) -> None:
        staged.json_text = self.old_json_text
        router._structured_schema_for_prompt = self.old_schema_adapter
        self.time_sleep.stop()
        self.time_monotonic.stop()
        self.cache.stop()
        self.env.stop()
        self.tmp.cleanup()

    @staticmethod
    def _second_pass_spec() -> contract.PlanningStageSpec:
        base = contract.script_stage_spec("full_script", ["s1"])
        policy = dataclasses.replace(
            base.provider_policy,
            max_attempts_per_provider=1,
            max_total_attempts=6,
            second_pass_after_full_exhaustion=True,
        )
        return dataclasses.replace(base, provider_policy=policy)

    def test_truncation_is_not_hard_capacity_but_is_not_retry_eligible(self) -> None:
        spec = self._second_pass_spec()
        bound = contract.bind_request_contract(spec, "prompt")
        classified, retryable, _retry_after, _failure = contract._provider_failure(
            bound,
            "gemini",
            RuntimeError(_GEMINI_OUTPUT_TRUNCATED),
        )
        self.assertEqual(classified.code, contract.PlanningErrorCode.PROVIDER_TRANSIENT)
        self.assertFalse(retryable)

    def test_collision_retries_only_retryable_groq_after_all_families_are_tried(self) -> None:
        valid = _script(["s1"])
        calls = {"gemini": 0, "groq": 0, "openrouter": 0}
        order: list[str] = []

        def fake_gemini(*_args, **_kwargs):
            calls["gemini"] += 1
            order.append("gemini")
            raise RuntimeError(_GEMINI_OUTPUT_TRUNCATED)

        def fake_groq(_prompt):
            calls["groq"] += 1
            order.append("groq")
            if calls["groq"] == 1:
                raise RuntimeError(_GROQ_JSON_VALIDATE_FAILED)
            return valid

        def fake_openrouter(*_args, **_kwargs):
            calls["openrouter"] += 1
            order.append("openrouter")
            raise RuntimeError(_OPENROUTER_SPEND_BLOCKED)

        with patch.object(router, "gemini_json_text", side_effect=fake_gemini), \
                patch.object(router, "_groq_call", side_effect=fake_groq), \
                patch.object(router, "_openrouter_call_with_repair", side_effect=fake_openrouter), \
                contract.request_stage_scope(self._second_pass_spec()):
            result = staged.json_text("unused", "prompt")

        self.assertEqual(result, valid)
        self.assertEqual(calls, {"gemini": 1, "groq": 2, "openrouter": 1})
        self.assertEqual(order, ["gemini", "groq", "openrouter", "groq"])
        self.contract_sleep_mock.assert_any_call(router.TRANSIENT_PROVIDER_COOLDOWN_SECONDS)

    def test_terminal_failure_does_not_suppress_other_provider_transient_retry(self) -> None:
        calls = {"gemini": 0, "groq": 0, "openrouter": 0}

        def fake_gemini(*_args, **_kwargs):
            calls["gemini"] += 1
            raise RuntimeError("GEMINI_HTTP_429 quota exhausted")

        def fake_groq(_prompt):
            calls["groq"] += 1
            raise RuntimeError(_GROQ_JSON_VALIDATE_FAILED)

        def fake_openrouter(*_args, **_kwargs):
            calls["openrouter"] += 1
            raise RuntimeError(_OPENROUTER_SPEND_BLOCKED)

        with patch.object(router, "gemini_json_text", side_effect=fake_gemini), \
                patch.object(router, "_groq_call", side_effect=fake_groq), \
                patch.object(router, "_openrouter_call_with_repair", side_effect=fake_openrouter), \
                contract.request_stage_scope(self._second_pass_spec()):
            with self.assertRaisesRegex(contract.PlanningStageError, r"exhausted after 4/6 attempts"):
                staged.json_text("unused", "prompt")

        self.assertEqual(calls, {"gemini": 1, "groq": 2, "openrouter": 1})
        self.contract_sleep_mock.assert_any_call(router.TRANSIENT_PROVIDER_COOLDOWN_SECONDS)


if __name__ == "__main__":
    unittest.main()

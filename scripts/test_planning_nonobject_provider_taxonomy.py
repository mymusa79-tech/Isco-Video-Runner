from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import isco_video_agent.resilient_planner as staged

from scripts import checkpoint_namespace_guard as checkpoint_guard
from scripts import planning_stage_contract as contract
from scripts import task_level_planner_router as router


class Run267ProviderTaxonomyTests(unittest.TestCase):
    """Targeted closure for Run #267 planning.full_script provider taxonomy only."""

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
        self.sleep = patch.object(contract.time, "sleep")
        self.sleep.start()
        self.router_sleep = patch.object(router.time, "sleep")
        self.router_sleep.start()
        self.old_json_text = staged.json_text
        self.old_schema_adapter = router._structured_schema_for_prompt
        staged.json_text = lambda *_args, **_kwargs: {}
        router._USED_PROVIDERS.clear()
        router._TELEMETRY.clear()

    def tearDown(self) -> None:
        staged.json_text = self.old_json_text
        router._structured_schema_for_prompt = self.old_schema_adapter
        self.router_sleep.stop()
        self.sleep.stop()
        self.cache.stop()
        self.env.stop()
        self.tmp.cleanup()

    def _install(self) -> None:
        checkpoint_guard.install_checkpoint_namespace_guard()
        checkpoint_guard.assert_stage_checkpoint_namespace_guarded()
        contract.install_planning_contract_router()

    def _groq_only_spec(
        self,
        *,
        max_attempts_per_provider: int = 2,
        max_total_attempts: int = 2,
        second_pass_after_full_exhaustion: bool = False,
    ) -> contract.PlanningStageSpec:
        base = contract.script_stage_spec("full_script", ["s1"])
        return contract.PlanningStageSpec(
            stage_id=base.stage_id,
            contract_id=base.contract_id,
            output_schema=base.output_schema,
            semantic_rules=base.semantic_rules,
            provider_policy=contract.ProviderPolicy(
                providers=("groq",),
                max_attempts_per_provider=max_attempts_per_provider,
                max_total_attempts=max_total_attempts,
                completion_tokens=base.provider_policy.completion_tokens,
                max_prompt_utf8_bytes=(),
                second_pass_after_full_exhaustion=second_pass_after_full_exhaustion,
            ),
            cache_policy=base.cache_policy,
        )

    def test_non_object_root_is_structural_one_wire_attempt_without_transient_retry_or_circuit(self) -> None:
        calls = 0

        def groq_non_object(_prompt):
            nonlocal calls
            calls += 1
            # Reproduce the real provider-helper boundary from Run #267: the HTTP call
            # already succeeded, then parsing syntactically valid JSON found a list root.
            router._parse_json("[]")
            raise AssertionError("unreachable")

        self._install()
        spec = self._groq_only_spec()
        with patch.object(router, "_groq_call", side_effect=groq_non_object):
            with contract.request_stage_scope(spec):
                with self.assertRaises(contract.PlanningStageError) as first:
                    staged.json_text("unused", "run267 non-object root first request")
            self.assertEqual(first.exception.code, contract.PlanningErrorCode.STRUCTURAL_INVALID)
            self.assertNotIn("PROVIDER_TRANSIENT", str(first.exception))
            self.assertIn("after 1/2 attempts (wire_only=true)", str(first.exception))
            self.assertEqual(calls, 1, "structural output must not receive a transient retry")

            # A structural response failure is request-local. Prove no run-scoped circuit
            # was opened by allowing a separate request to contact Groq again.
            with contract.request_stage_scope(spec):
                with self.assertRaises(contract.PlanningStageError) as second:
                    staged.json_text("unused", "run267 non-object root second request")
            self.assertEqual(second.exception.code, contract.PlanningErrorCode.STRUCTURAL_INVALID)
            self.assertEqual(calls, 2)

        events = [item for item in router.get_telemetry() if item.get("provider") == "groq"]
        self.assertEqual(len(events), 2)
        self.assertTrue(all(item.get("wire_attempted") is True for item in events))
        self.assertTrue(all(item.get("provider_attempt") == 1 for item in events))
        self.assertFalse(any(item.get("result") == "circuit-open" for item in events))

    def test_malformed_json_keeps_existing_nonretryable_provider_transient_behavior(self) -> None:
        error = RuntimeError("Provider returned invalid JSON")
        stage_error, retryable, _retry_after, failure = contract._provider_failure(
            self._groq_only_spec(), "groq", error
        )
        self.assertEqual(stage_error.code, contract.PlanningErrorCode.PROVIDER_TRANSIENT)
        self.assertFalse(retryable)
        self.assertEqual(failure.telemetry_result, "invalid_json")
        self.assertFalse(failure.open_circuit)

    def test_real_timeout_remains_provider_transient_and_retryable(self) -> None:
        stage_error, retryable, _retry_after, failure = contract._provider_failure(
            self._groq_only_spec(), "groq", RuntimeError("request timed out")
        )
        self.assertEqual(stage_error.code, contract.PlanningErrorCode.PROVIDER_TRANSIENT)
        self.assertTrue(retryable)
        self.assertEqual(failure.telemetry_result, "timeout")
        self.assertFalse(failure.open_circuit)

    def test_capacity_and_quota_taxonomy_is_unchanged(self) -> None:
        payload_error, payload_retryable, _retry_after, payload_failure = contract._provider_failure(
            self._groq_only_spec(),
            "groq",
            RuntimeError("GROQ_PAYLOAD_TOO_LARGE_PREFLIGHT prompt_bytes=30000 limit=28672"),
        )
        self.assertEqual(payload_error.code, contract.PlanningErrorCode.CAPACITY)
        self.assertFalse(payload_retryable)
        self.assertEqual(payload_failure.telemetry_result, "payload_too_large")

        window_error, window_retryable, _retry_after, window_failure = contract._provider_failure(
            self._groq_only_spec(),
            "groq",
            RuntimeError(
                "GROQ_TPM_WINDOW_BUSY_PRECHECK required_estimate=6332 remaining=2581 reset_in=39.75s"
            ),
        )
        self.assertEqual(window_error.code, contract.PlanningErrorCode.CAPACITY)
        self.assertTrue(window_retryable)
        self.assertFalse(window_failure.open_circuit)

        short_error, short_retryable, short_retry_after, short_failure = contract._provider_failure(
            self._groq_only_spec(),
            "gemini",
            RuntimeError("GEMINI_HTTP_429 status=429 rate limit tokens per minute Retry-After=5"),
        )
        self.assertEqual(short_error.code, contract.PlanningErrorCode.CAPACITY)
        self.assertFalse(short_retryable)
        self.assertEqual(short_failure.telemetry_result, "429")
        self.assertEqual(short_failure.quota_scope, "short_window")
        self.assertEqual(short_retry_after, 5.0)
        self.assertFalse(short_failure.open_circuit)

        daily_error, daily_retryable, _retry_after, daily_failure = contract._provider_failure(
            self._groq_only_spec(),
            "gemini",
            RuntimeError("GEMINI_HTTP_429 status=429 quota exceeded requests per day"),
        )
        self.assertEqual(daily_error.code, contract.PlanningErrorCode.CAPACITY)
        self.assertFalse(daily_retryable)
        self.assertEqual(daily_failure.quota_scope, "daily")
        self.assertTrue(daily_failure.open_circuit)

    def test_groq_tpm_window_precheck_remains_no_wire_and_does_not_advance_total_attempts(self) -> None:
        calls = 0

        def groq_no_wire(_prompt):
            nonlocal calls
            calls += 1
            raise router.NoWireProviderFailure(
                "GROQ_TPM_WINDOW_BUSY_PRECHECK",
                "required_estimate=6332 remaining=2581 reset_in=39.75s",
            )

        self._install()
        spec = self._groq_only_spec()
        with patch.object(router, "_groq_call", side_effect=groq_no_wire), \
                contract.request_stage_scope(spec):
            with self.assertRaisesRegex(
                contract.PlanningStageError,
                r"after 0/2 attempts \(wire_only=true\)",
            ):
                staged.json_text("unused", "run267 no-wire accounting")

        self.assertEqual(calls, 1)
        event = next(item for item in router.get_telemetry() if item.get("provider") == "groq")
        self.assertFalse(event["wire_attempted"])
        self.assertIsNone(event["provider_attempt"])


if __name__ == "__main__":
    unittest.main()

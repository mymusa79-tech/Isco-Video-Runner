from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import isco_video_agent.resilient_planner as staged

from scripts import checkpoint_namespace_guard as checkpoint_guard
from scripts import planning_stage_contract as stage_contract
from scripts import run124_terminal_provider_recovery as recovery
from scripts import task_level_planner_router as router
from scripts.provider_failure import classify_provider_failure as base_classify_provider_failure


MODEL = "openai/gpt-oss-120b"
FIRST_RESET = 31.98
SECOND_RESET = 32.30
FIRST_WAIT = 33.48
SECOND_WAIT = 33.80
WIRE_FAILURE = RuntimeError(
    "GROQ_JSON_VALIDATE_FAILED status=400 code=json_validate_failed"
)


def _busy(reset_seconds: float) -> router.NoWireProviderFailure:
    return router.NoWireProviderFailure(
        "GROQ_TPM_WINDOW_BUSY_PRECHECK",
        f"model={MODEL} required_estimate=6161 remaining=3200 "
        f"reset_in={reset_seconds:.2f}s action=failover_without_http",
    )


def _valid() -> dict:
    return {
        "additions": [
            {"id": "s1", "append_text": "إضافة صالحة ومحدودة تحافظ على المعنى"},
        ]
    }


class Run271AppendPostWireTemporalRecoveryTests(unittest.TestCase):
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

        self.old_json_text = staged.json_text
        self.old_schema_adapter = router._structured_schema_for_prompt
        self.old_provider_result = stage_contract._provider_result
        staged.json_text = lambda *_args, **_kwargs: {}
        router._TELEMETRY.clear()
        router._USED_PROVIDERS.clear()

        self.classifier = patch.object(
            router, "classify_provider_failure", base_classify_provider_failure
        )
        self.classifier.start()
        self.min_interval = patch.object(router, "MIN_PROVIDER_CALL_INTERVAL_SECONDS", 0.0)
        self.min_interval.start()
        self.retry_delay = patch.object(router, "_retry_delay_seconds", return_value=0.0)
        self.retry_delay.start()
        self.clear_window = patch.object(recovery, "_clear_waited_model_window", return_value=None)
        self.clear_window.start()

        recovery._RECOVERED_TERMINAL_SHARDS.clear()
        recovery._WAITED_APPEND_STAGE_WINDOWS.clear()
        recovery._APPEND_REAL_WIRE_GENERATION.clear()
        recovery._TERMINAL_RECOVERY_COUNT = 0
        recovery._TERMINAL_WAIT_SPENT_SECONDS = 0.0

        checkpoint_guard.install_checkpoint_namespace_guard()
        checkpoint_guard.assert_stage_checkpoint_namespace_guarded()
        stage_contract.install_planning_contract_router()

    def tearDown(self) -> None:
        stage_contract._provider_result = self.old_provider_result
        staged.json_text = self.old_json_text
        router._structured_schema_for_prompt = self.old_schema_adapter
        router._TELEMETRY.clear()
        router._USED_PROVIDERS.clear()
        recovery._RECOVERED_TERMINAL_SHARDS.clear()
        recovery._WAITED_APPEND_STAGE_WINDOWS.clear()
        recovery._APPEND_REAL_WIRE_GENERATION.clear()
        recovery._TERMINAL_RECOVERY_COUNT = 0
        recovery._TERMINAL_WAIT_SPENT_SECONDS = 0.0
        self.clear_window.stop()
        self.retry_delay.stop()
        self.min_interval.stop()
        self.classifier.stop()
        self.cache.stop()
        self.env.stop()
        self.tmp.cleanup()

    def _spec(self, *, max_per_provider: int = 2, max_total: int = 8):
        base = stage_contract.append_stage_spec(["s1"])
        return replace(
            base,
            provider_policy=replace(
                base.provider_policy,
                providers=("groq",),
                max_attempts_per_provider=max_per_provider,
                max_total_attempts=max_total,
                second_pass_after_full_exhaustion=False,
            ),
        )

    def _run(self, provider_result, *, max_per_provider: int = 2, max_total: int = 8):
        stage_contract._provider_result = provider_result
        recovery._install_stage_temporal_capacity_bridge()
        with stage_contract.request_stage_scope(
            self._spec(
                max_per_provider=max_per_provider,
                max_total=max_total,
            )
        ):
            return staged.json_text(
                "unused-primary-key",
                "opaque append prompt",
                model="gemini-3.7-flash",
            )

    def test_before_fix_request_global_wait_cap_reproduces_run271_one_of_eight_exhaustion(self) -> None:
        calls: list[str] = []

        def provider_result(provider, *_args, **_kwargs):
            self.assertEqual(provider, "groq")
            step = len(calls)
            if step == 0:
                calls.append("prewire-1")
                raise _busy(FIRST_RESET)
            if step == 1:
                calls.append("wire-1")
                raise WIRE_FAILURE
            if step == 2:
                calls.append("prewire-2")
                raise _busy(SECOND_RESET)
            calls.append("wire-2")
            return _valid()

        # Pre-fix semantics were request-global: every temporal precheck read the same
        # generation (0), so the second one was denied even though wire #1 occurred.
        with patch.object(recovery, "_append_real_wire_generation", return_value=0), \
                patch.object(recovery.time, "sleep") as sleep:
            with self.assertRaises(stage_contract.PlanningStageError) as captured:
                self._run(provider_result)

        self.assertIn("all providers exhausted after 1/8 attempts", str(captured.exception))
        self.assertEqual(calls, ["prewire-1", "wire-1", "prewire-2"])
        self.assertEqual(sleep.call_count, 2)  # temporal wait + existing retry delay(0)
        self.assertAlmostEqual(sleep.call_args_list[0].args[0], FIRST_WAIT, places=2)
        telemetry = [row for row in router.get_telemetry() if row.get("provider") == "groq"]
        self.assertEqual(len(telemetry), 2)
        self.assertTrue(telemetry[0]["wire_attempted"])
        self.assertEqual(telemetry[0]["provider_attempt"], 1)
        self.assertFalse(telemetry[1]["wire_attempted"])
        self.assertIsNone(telemetry[1]["provider_attempt"])

    def test_exact_run271_real_wire_opens_second_existing_temporal_opportunity(self) -> None:
        calls: list[str] = []
        capture = io.StringIO()

        def provider_result(provider, *_args, **_kwargs):
            self.assertEqual(provider, "groq")
            step = len(calls)
            if step == 0:
                calls.append("prewire-1")
                raise _busy(FIRST_RESET)
            if step == 1:
                calls.append("wire-1")
                raise WIRE_FAILURE
            if step == 2:
                calls.append("prewire-2")
                raise _busy(SECOND_RESET)
            calls.append("wire-2")
            return _valid()

        with patch.object(recovery.time, "sleep") as sleep, contextlib.redirect_stdout(capture):
            result = self._run(provider_result)

        self.assertEqual(result, _valid())
        self.assertEqual(calls, ["prewire-1", "wire-1", "prewire-2", "wire-2"])

        temporal_waits = [
            call.args[0]
            for call in sleep.call_args_list
            if call.args and float(call.args[0]) >= 30.0
        ]
        self.assertEqual(len(temporal_waits), 2)
        self.assertAlmostEqual(temporal_waits[0], FIRST_WAIT, places=2)
        self.assertAlmostEqual(temporal_waits[1], SECOND_WAIT, places=2)
        self.assertAlmostEqual(
            recovery._TERMINAL_WAIT_SPENT_SECONDS,
            FIRST_WAIT + SECOND_WAIT,
            places=2,
        )

        output = capture.getvalue()
        self.assertIn("append_temporal_wait_authorized=true", output)
        self.assertIn("reason=new_existing_wire_opportunity", output)
        self.assertIn("prior_real_wire_attempt=1", output)
        self.assertIn("next_wire_attempt=2", output)
        self.assertIn("append_real_wire_observed=true", output)

        telemetry = [row for row in router.get_telemetry() if row.get("provider") == "groq"]
        self.assertEqual(len(telemetry), 2, telemetry)
        self.assertEqual(telemetry[0]["result"], "generation_error")
        self.assertTrue(telemetry[0]["wire_attempted"])
        self.assertEqual(telemetry[0]["provider_attempt"], 1)
        self.assertEqual(telemetry[1]["result"], "success")
        self.assertTrue(telemetry[1]["wire_attempted"])
        self.assertEqual(telemetry[1]["provider_attempt"], 2)

    def test_repeated_prewire_window_without_intervening_real_wire_never_waits_twice(self) -> None:
        calls = 0

        def provider_result(provider, *_args, **_kwargs):
            nonlocal calls
            self.assertEqual(provider, "groq")
            calls += 1
            raise _busy(FIRST_RESET if calls == 1 else SECOND_RESET)

        with patch.object(recovery.time, "sleep") as sleep:
            with self.assertRaises(stage_contract.PlanningStageError):
                self._run(provider_result)

        self.assertEqual(calls, 2)
        temporal_waits = [
            call.args[0]
            for call in sleep.call_args_list
            if call.args and float(call.args[0]) >= 30.0
        ]
        self.assertEqual(len(temporal_waits), 1)
        self.assertAlmostEqual(temporal_waits[0], FIRST_WAIT, places=2)
        self.assertEqual(recovery._APPEND_REAL_WIRE_GENERATION, {})
        telemetry = [row for row in router.get_telemetry() if row.get("provider") == "groq"]
        self.assertEqual(len(telemetry), 1)
        self.assertFalse(telemetry[0]["wire_attempted"])
        self.assertIsNone(telemetry[0]["provider_attempt"])

    def test_max_provider_attempts_exhausted_does_not_create_second_temporal_opportunity(self) -> None:
        calls: list[str] = []

        def provider_result(provider, *_args, **_kwargs):
            self.assertEqual(provider, "groq")
            if not calls:
                calls.append("prewire-1")
                raise _busy(FIRST_RESET)
            calls.append("wire-1")
            raise WIRE_FAILURE

        with patch.object(recovery.time, "sleep") as sleep:
            with self.assertRaises(stage_contract.PlanningStageError) as captured:
                self._run(provider_result, max_per_provider=1, max_total=8)

        self.assertIn("all providers exhausted after 1/8 attempts", str(captured.exception))
        self.assertEqual(calls, ["prewire-1", "wire-1"])
        temporal_waits = [
            call.args[0]
            for call in sleep.call_args_list
            if call.args and float(call.args[0]) >= 30.0
        ]
        self.assertEqual(len(temporal_waits), 1)
        self.assertAlmostEqual(temporal_waits[0], FIRST_WAIT, places=2)
        self.assertEqual(sum(recovery._APPEND_REAL_WIRE_GENERATION.values()), 1)

    def test_max_total_attempts_exhausted_does_not_create_second_temporal_opportunity(self) -> None:
        calls: list[str] = []

        def provider_result(provider, *_args, **_kwargs):
            self.assertEqual(provider, "groq")
            if not calls:
                calls.append("prewire-1")
                raise _busy(FIRST_RESET)
            calls.append("wire-1")
            raise WIRE_FAILURE

        with patch.object(recovery.time, "sleep") as sleep:
            with self.assertRaises(stage_contract.PlanningStageError) as captured:
                self._run(provider_result, max_per_provider=2, max_total=1)

        self.assertIn("all providers exhausted after 1/1 attempts", str(captured.exception))
        self.assertEqual(calls, ["prewire-1", "wire-1"])
        temporal_waits = [
            call.args[0]
            for call in sleep.call_args_list
            if call.args and float(call.args[0]) >= 30.0
        ]
        self.assertEqual(len(temporal_waits), 1)
        self.assertAlmostEqual(temporal_waits[0], FIRST_WAIT, places=2)
        self.assertEqual(sum(recovery._APPEND_REAL_WIRE_GENERATION.values()), 1)

    def test_run_wide_wait_budget_still_denies_temporal_pacing(self) -> None:
        calls = 0

        def provider_result(provider, *_args, **_kwargs):
            nonlocal calls
            self.assertEqual(provider, "groq")
            calls += 1
            raise _busy(FIRST_RESET)

        recovery._TERMINAL_WAIT_SPENT_SECONDS = (
            recovery._MAX_TERMINAL_WAIT_SECONDS_PER_RUN - 10.0
        )
        with patch.object(recovery.time, "sleep") as sleep:
            with self.assertRaises(stage_contract.PlanningStageError):
                self._run(provider_result)

        self.assertEqual(calls, 1)
        sleep.assert_not_called()
        self.assertEqual(recovery._APPEND_REAL_WIRE_GENERATION, {})


if __name__ == "__main__":
    unittest.main()

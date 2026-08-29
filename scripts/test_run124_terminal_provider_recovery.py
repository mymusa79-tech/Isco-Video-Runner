from __future__ import annotations

import unittest
from unittest.mock import patch

from scripts import run124_terminal_provider_recovery as recovery


MODEL = "openai/gpt-oss-120b"
_FAILURE = (
    "All free providers failed for planning subtask: "
    "gemini:timeout | "
    f"groq:GROQ_TPM_WINDOW_BUSY_PRECHECK model={MODEL} required_estimate=5928 "
    "remaining=3039 reset_in=36.88s action=failover_without_http | "
    "openrouter:OPENROUTER_PREMATURE_RESPONSE finish_reason=length"
)


class Run124TerminalProviderRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_call = recovery.batching._call_capacity_aware_shard
        self.original_markers = recovery.batching._TRANSPORT_PRESSURE_MARKERS
        self.had_flag = hasattr(recovery.batching, "_ISCO_RUN124_TERMINAL_PROVIDER_RECOVERY")
        self.original_flag = getattr(
            recovery.batching,
            "_ISCO_RUN124_TERMINAL_PROVIDER_RECOVERY",
            None,
        )
        if self.had_flag:
            delattr(recovery.batching, "_ISCO_RUN124_TERMINAL_PROVIDER_RECOVERY")
        recovery.capacity.reset_groq_capacity_state_for_tests()
        recovery._RECOVERED_TERMINAL_SHARDS.clear()
        recovery._TERMINAL_RECOVERY_COUNT = 0
        recovery._TERMINAL_WAIT_SPENT_SECONDS = 0.0

    def tearDown(self) -> None:
        recovery.batching._call_capacity_aware_shard = self.original_call
        recovery.batching._TRANSPORT_PRESSURE_MARKERS = self.original_markers
        recovery.capacity.reset_groq_capacity_state_for_tests()
        recovery._RECOVERED_TERMINAL_SHARDS.clear()
        recovery._TERMINAL_RECOVERY_COUNT = 0
        recovery._TERMINAL_WAIT_SPENT_SECONDS = 0.0
        if self.had_flag:
            recovery.batching._ISCO_RUN124_TERMINAL_PROVIDER_RECOVERY = self.original_flag
        elif hasattr(recovery.batching, "_ISCO_RUN124_TERMINAL_PROVIDER_RECOVERY"):
            delattr(recovery.batching, "_ISCO_RUN124_TERMINAL_PROVIDER_RECOVERY")

    def test_run124_log_shape_is_recoverable_from_exact_failure_evidence(self) -> None:
        self.assertAlmostEqual(
            recovery._remaining_reset_seconds(RuntimeError(_FAILURE)),
            36.88,
            places=2,
        )
        self.assertEqual(recovery._model_from_error(RuntimeError(_FAILURE)), MODEL)

    def test_terminal_single_section_waits_once_then_clears_only_failed_model(self) -> None:
        calls: list[tuple[str, ...]] = []

        def fake_call(_api_key, _model, ids, *, prompt_builder, label):
            del prompt_builder, label
            calls.append(tuple(ids))
            if len(calls) == 1:
                raise RuntimeError(_FAILURE)
            return {ids[0]: {"id": ids[0], "narration": "ok", "key_point": "ok"}}

        recovery.batching._call_capacity_aware_shard = fake_call
        failed = recovery.capacity._model_state(MODEL)
        failed["remaining_tokens"] = 3039
        failed["reset_at_epoch"] = 1234.0
        other_model = "openai/gpt-oss-20b"
        other = recovery.capacity._model_state(other_model)
        other["remaining_tokens"] = 777
        other["reset_at_epoch"] = 9999.0
        recovery.install_run124_terminal_provider_recovery()

        with patch.object(recovery.time, "sleep") as sleep:
            result = recovery.batching._call_capacity_aware_shard(
                "key",
                "model",
                ["S1"],
                prompt_builder=lambda _ids: "prompt",
                label="writer",
            )

        self.assertEqual(result["S1"]["narration"], "ok")
        self.assertEqual(calls, [("S1",), ("S1",)])
        sleep.assert_called_once_with(38.38)
        self.assertIsNone(recovery.capacity._model_state(MODEL)["remaining_tokens"])
        self.assertIsNone(recovery.capacity._model_state(MODEL)["reset_at_epoch"])
        self.assertEqual(recovery.capacity._model_state(other_model)["remaining_tokens"], 777)
        self.assertEqual(recovery.capacity._model_state(other_model)["reset_at_epoch"], 9999.0)

    def test_near_limit_reset_remains_recoverable_and_actual_sleep_is_capped(self) -> None:
        near_limit_failure = RuntimeError(
            _FAILURE.replace("reset_in=36.88s", "reset_in=59.50s")
        )
        calls = 0

        def fake_call(_api_key, _model, ids, *, prompt_builder, label):
            nonlocal calls
            del prompt_builder, label
            calls += 1
            if calls == 1:
                raise near_limit_failure
            return {ids[0]: {"id": ids[0], "narration": "ok", "key_point": "ok"}}

        self.assertAlmostEqual(
            recovery._remaining_reset_seconds(near_limit_failure),
            59.50,
            places=2,
        )
        recovery.batching._call_capacity_aware_shard = fake_call
        recovery.install_run124_terminal_provider_recovery()

        with patch.object(recovery.time, "sleep") as sleep:
            result = recovery.batching._call_capacity_aware_shard(
                "key",
                "model",
                ["S-limit"],
                prompt_builder=lambda _ids: "prompt",
                label="writer",
            )

        self.assertEqual(result["S-limit"]["narration"], "ok")
        self.assertEqual(calls, 2)
        sleep.assert_called_once_with(60.0)
        self.assertEqual(recovery._TERMINAL_RECOVERY_COUNT, 1)
        self.assertEqual(recovery._TERMINAL_WAIT_SPENT_SECONDS, 60.0)

    def test_run132_two_legitimate_reset_windows_fit_the_run_budget(self) -> None:
        """Run132 regression: ~49s then ~41s must not contradict recovery_cap=3."""
        attempts: dict[str, int] = {}
        reset_by_id = {"S7": 47.88, "S8": 39.18}

        def fake_call(_api_key, _model, ids, *, prompt_builder, label):
            del prompt_builder, label
            section_id = ids[0]
            attempts[section_id] = attempts.get(section_id, 0) + 1
            if attempts[section_id] == 1:
                raise RuntimeError(
                    _FAILURE.replace("reset_in=36.88s", f"reset_in={reset_by_id[section_id]:.2f}s")
                )
            return {section_id: {"id": section_id, "narration": "ok", "key_point": "ok"}}

        recovery.batching._call_capacity_aware_shard = fake_call
        recovery.install_run124_terminal_provider_recovery()

        with patch.object(recovery.time, "sleep") as sleep:
            for section_id in ("S7", "S8"):
                result = recovery.batching._call_capacity_aware_shard(
                    "key",
                    "model",
                    [section_id],
                    prompt_builder=lambda _ids: "prompt",
                    label="writer",
                )
                self.assertEqual(result[section_id]["narration"], "ok")

        self.assertEqual(attempts, {"S7": 2, "S8": 2})
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [49.38, 40.68])
        self.assertEqual(recovery._TERMINAL_RECOVERY_COUNT, 2)
        self.assertAlmostEqual(recovery._TERMINAL_WAIT_SPENT_SECONDS, 90.06, places=2)
        self.assertLessEqual(
            recovery._TERMINAL_WAIT_SPENT_SECONDS,
            recovery._MAX_TERMINAL_WAIT_SECONDS_PER_RUN,
        )

    def test_run_wide_recovery_count_remains_hard_bounded(self) -> None:
        recovery._TERMINAL_RECOVERY_COUNT = recovery._MAX_TERMINAL_RECOVERIES_PER_RUN
        self.assertFalse(recovery._run_wait_budget_allows(1.0))

    def test_terminal_recovery_never_loops_if_retry_still_fails(self) -> None:
        calls = 0

        def always_fail(_api_key, _model, ids, *, prompt_builder, label):
            nonlocal calls
            del ids, prompt_builder, label
            calls += 1
            raise RuntimeError(_FAILURE)

        recovery.batching._call_capacity_aware_shard = always_fail
        recovery.install_run124_terminal_provider_recovery()

        with patch.object(recovery.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "All free providers failed"):
                recovery.batching._call_capacity_aware_shard(
                    "key",
                    "model",
                    ["S1"],
                    prompt_builder=lambda _ids: "prompt",
                    label="writer",
                )

        self.assertEqual(calls, 2)
        self.assertEqual(sleep.call_count, 1)

    def test_reset_farther_than_one_minute_preserves_fail_fast_behavior(self) -> None:
        far_failure = RuntimeError(_FAILURE.replace("reset_in=36.88s", "reset_in=61.01s"))
        calls = 0

        def fail(_api_key, _model, ids, *, prompt_builder, label):
            nonlocal calls
            del ids, prompt_builder, label
            calls += 1
            raise far_failure

        recovery.batching._call_capacity_aware_shard = fail
        recovery.install_run124_terminal_provider_recovery()

        with patch.object(recovery.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "All free providers failed"):
                recovery.batching._call_capacity_aware_shard(
                    "key",
                    "model",
                    ["S1"],
                    prompt_builder=lambda _ids: "prompt",
                    label="writer",
                )

        self.assertEqual(calls, 1)
        sleep.assert_not_called()

    def test_multi_section_failure_does_not_use_terminal_wait(self) -> None:
        calls = 0

        def fail(_api_key, _model, ids, *, prompt_builder, label):
            nonlocal calls
            del ids, prompt_builder, label
            calls += 1
            raise RuntimeError(_FAILURE)

        recovery.batching._call_capacity_aware_shard = fail
        recovery.install_run124_terminal_provider_recovery()

        with patch.object(recovery.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "All free providers failed"):
                recovery.batching._call_capacity_aware_shard(
                    "key",
                    "model",
                    ["S1", "S2"],
                    prompt_builder=lambda _ids: "prompt",
                    label="writer",
                )

        self.assertEqual(calls, 1)
        sleep.assert_not_called()

    def test_installer_classifies_busy_groq_window_as_transport_pressure(self) -> None:
        recovery.install_run124_terminal_provider_recovery()
        self.assertIn(
            "groq_tpm_window_busy_precheck",
            recovery.batching._TRANSPORT_PRESSURE_MARKERS,
        )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
from unittest.mock import patch

from scripts import run124_terminal_provider_recovery as recovery


_FAILURE = (
    "All free providers failed for planning subtask: "
    "gemini:timeout | "
    "groq:GROQ_TPM_WINDOW_BUSY_PRECHECK required_estimate=5928 remaining=3039 "
    "reset_in=36.88s action=failover_without_http | "
    "openrouter:OPENROUTER_PREMATURE_RESPONSE finish_reason=length"
)


class Run124TerminalProviderRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_call = recovery.batching._call_capacity_aware_shard
        self.original_markers = recovery.batching._TRANSPORT_PRESSURE_MARKERS
        self.original_rate_state = dict(recovery.capacity._GROQ_RATE_STATE)
        self.had_flag = hasattr(recovery.batching, "_ISCO_RUN124_TERMINAL_PROVIDER_RECOVERY")
        self.original_flag = getattr(
            recovery.batching,
            "_ISCO_RUN124_TERMINAL_PROVIDER_RECOVERY",
            None,
        )
        if self.had_flag:
            delattr(recovery.batching, "_ISCO_RUN124_TERMINAL_PROVIDER_RECOVERY")
        recovery._RECOVERED_TERMINAL_SHARDS.clear()

    def tearDown(self) -> None:
        recovery.batching._call_capacity_aware_shard = self.original_call
        recovery.batching._TRANSPORT_PRESSURE_MARKERS = self.original_markers
        recovery.capacity._GROQ_RATE_STATE.clear()
        recovery.capacity._GROQ_RATE_STATE.update(self.original_rate_state)
        recovery._RECOVERED_TERMINAL_SHARDS.clear()
        if self.had_flag:
            recovery.batching._ISCO_RUN124_TERMINAL_PROVIDER_RECOVERY = self.original_flag
        elif hasattr(recovery.batching, "_ISCO_RUN124_TERMINAL_PROVIDER_RECOVERY"):
            delattr(recovery.batching, "_ISCO_RUN124_TERMINAL_PROVIDER_RECOVERY")

    def test_run124_log_shape_is_recoverable_from_authoritative_reset_state(self) -> None:
        recovery.capacity._GROQ_RATE_STATE["remaining_tokens"] = 3039
        recovery.capacity._GROQ_RATE_STATE["reset_at_monotonic"] = 136.88
        with patch.object(recovery.time, "monotonic", return_value=100.0):
            self.assertAlmostEqual(
                recovery._remaining_reset_seconds(RuntimeError(_FAILURE)),
                36.88,
                places=2,
            )

    def test_terminal_single_section_waits_once_then_retries(self) -> None:
        calls: list[tuple[str, ...]] = []

        def fake_call(_api_key, _model, ids, *, prompt_builder, label):
            del prompt_builder, label
            calls.append(tuple(ids))
            if len(calls) == 1:
                raise RuntimeError(_FAILURE)
            return {ids[0]: {"id": ids[0], "narration": "ok", "key_point": "ok"}}

        recovery.batching._call_capacity_aware_shard = fake_call
        recovery.capacity._GROQ_RATE_STATE["remaining_tokens"] = 3039
        recovery.capacity._GROQ_RATE_STATE["reset_at_monotonic"] = 110.0
        recovery.install_run124_terminal_provider_recovery()

        with patch.object(recovery.time, "monotonic", return_value=100.0), patch.object(
            recovery.time,
            "sleep",
        ) as sleep:
            result = recovery.batching._call_capacity_aware_shard(
                "key",
                "model",
                ["S1"],
                prompt_builder=lambda _ids: "prompt",
                label="writer",
            )

        self.assertEqual(result["S1"]["narration"], "ok")
        self.assertEqual(calls, [("S1",), ("S1",)])
        sleep.assert_called_once_with(11.5)
        self.assertIsNone(recovery.capacity._GROQ_RATE_STATE["remaining_tokens"])
        self.assertIsNone(recovery.capacity._GROQ_RATE_STATE["reset_at_monotonic"])

    def test_terminal_recovery_never_loops_if_retry_still_fails(self) -> None:
        calls = 0

        def always_fail(_api_key, _model, ids, *, prompt_builder, label):
            nonlocal calls
            del ids, prompt_builder, label
            calls += 1
            raise RuntimeError(_FAILURE)

        recovery.batching._call_capacity_aware_shard = always_fail
        recovery.capacity._GROQ_RATE_STATE["remaining_tokens"] = 3039
        recovery.capacity._GROQ_RATE_STATE["reset_at_monotonic"] = 105.0
        recovery.install_run124_terminal_provider_recovery()

        with patch.object(recovery.time, "monotonic", return_value=100.0), patch.object(
            recovery.time,
            "sleep",
        ) as sleep:
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
        calls = 0

        def fail(_api_key, _model, ids, *, prompt_builder, label):
            nonlocal calls
            del ids, prompt_builder, label
            calls += 1
            raise RuntimeError(_FAILURE)

        recovery.batching._call_capacity_aware_shard = fail
        recovery.capacity._GROQ_RATE_STATE["remaining_tokens"] = 100
        recovery.capacity._GROQ_RATE_STATE["reset_at_monotonic"] = 161.0
        recovery.install_run124_terminal_provider_recovery()

        with patch.object(recovery.time, "monotonic", return_value=100.0), patch.object(
            recovery.time,
            "sleep",
        ) as sleep:
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
        recovery.capacity._GROQ_RATE_STATE["remaining_tokens"] = 3039
        recovery.capacity._GROQ_RATE_STATE["reset_at_monotonic"] = 105.0
        recovery.install_run124_terminal_provider_recovery()

        with patch.object(recovery.time, "monotonic", return_value=100.0), patch.object(
            recovery.time,
            "sleep",
        ) as sleep:
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

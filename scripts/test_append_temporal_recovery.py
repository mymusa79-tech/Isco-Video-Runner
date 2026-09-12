from __future__ import annotations

import unittest
from unittest.mock import patch

from scripts import planning_stage_contract as stage_contract
from scripts import run124_terminal_provider_recovery as recovery


MODEL = "openai/gpt-oss-120b"


def _temporal_failure(reset: float = 34.35) -> stage_contract.PlanningStageError:
    return stage_contract.PlanningStageError(
        stage_contract.PlanningErrorCode.CAPACITY,
        "GROQ_TPM_WINDOW_BUSY_PRECHECK "
        f"model={MODEL} remaining=3120 reset_in={reset:.2f}s "
        "action=failover_without_http",
        stage_id="planning.append_only_repair",
        provider="groq",
    )


class AppendTemporalRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_provider_result = stage_contract._provider_result
        recovery._RECOVERED_APPEND_REQUESTS.clear()
        recovery._TERMINAL_RECOVERY_COUNT = 0
        recovery._TERMINAL_WAIT_SPENT_SECONDS = 0.0
        recovery.capacity.reset_groq_capacity_state_for_tests()

    def tearDown(self) -> None:
        stage_contract._provider_result = self.original_provider_result
        recovery._RECOVERED_APPEND_REQUESTS.clear()
        recovery._TERMINAL_RECOVERY_COUNT = 0
        recovery._TERMINAL_WAIT_SPENT_SECONDS = 0.0
        recovery.capacity.reset_groq_capacity_state_for_tests()

    def _contract(self):
        spec = stage_contract.append_stage_spec(["S1"])
        return stage_contract.bind_request_contract(spec, "exact append prompt")

    def test_append_waits_for_proven_temporal_window_then_retries_exact_request_once(self) -> None:
        calls = 0

        def fake_provider_result(provider, prompt, model, contract, api_key):
            nonlocal calls
            calls += 1
            self.assertEqual(provider, "groq")
            self.assertEqual(prompt, "prompt")
            self.assertEqual(model, "model")
            self.assertEqual(api_key, "api-key")
            if calls == 1:
                raise _temporal_failure()
            return {"additions": [{"id": "S1", "append_text": "إضافة صالحة"}]}

        stage_contract._provider_result = fake_provider_result
        recovery._install_stage_temporal_capacity_bridge()
        contract = self._contract()
        token = stage_contract._ACTIVE_REQUEST_CONTRACT.set(contract)
        state = recovery.capacity._model_state(MODEL)
        state["remaining_tokens"] = 3120
        state["reset_at_epoch"] = 9999.0
        try:
            with patch.object(recovery.time, "sleep") as sleep:
                result = stage_contract._provider_result(
                    "groq", "prompt", "model", contract, "api-key"
                )
        finally:
            stage_contract._ACTIVE_REQUEST_CONTRACT.reset(token)

        self.assertEqual(result["additions"][0]["id"], "S1")
        self.assertEqual(calls, 2)
        sleep.assert_called_once_with(35.85)
        self.assertEqual(recovery._TERMINAL_RECOVERY_COUNT, 1)
        self.assertAlmostEqual(recovery._TERMINAL_WAIT_SPENT_SECONDS, 35.85, places=2)
        self.assertIsNone(recovery.capacity._model_state(MODEL)["remaining_tokens"])
        self.assertIsNone(recovery.capacity._model_state(MODEL)["reset_at_epoch"])

    def test_append_repeated_temporal_failure_waits_once_then_becomes_no_wire(self) -> None:
        calls = 0

        def always_temporal(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise _temporal_failure()

        stage_contract._provider_result = always_temporal
        recovery._install_stage_temporal_capacity_bridge()
        contract = self._contract()
        token = stage_contract._ACTIVE_REQUEST_CONTRACT.set(contract)
        try:
            with patch.object(recovery.time, "sleep") as sleep:
                with self.assertRaises(recovery.NoWireProviderFailure) as captured:
                    stage_contract._provider_result(
                        "groq", "prompt", "model", contract, "api-key"
                    )
                with self.assertRaises(recovery.NoWireProviderFailure):
                    stage_contract._provider_result(
                        "groq", "prompt", "model", contract, "api-key"
                    )
        finally:
            stage_contract._ACTIVE_REQUEST_CONTRACT.reset(token)

        self.assertEqual(captured.exception.reason_code, "temporal_capacity_window")
        self.assertEqual(calls, 3)
        self.assertEqual(sleep.call_count, 1)
        self.assertEqual(recovery._TERMINAL_RECOVERY_COUNT, 1)

    def test_append_recovery_respects_existing_run_wide_wait_budget(self) -> None:
        calls = 0

        def temporal(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise _temporal_failure()

        stage_contract._provider_result = temporal
        recovery._install_stage_temporal_capacity_bridge()
        contract = self._contract()
        recovery._TERMINAL_WAIT_SPENT_SECONDS = (
            recovery._MAX_TERMINAL_WAIT_SECONDS_PER_RUN - 10.0
        )
        token = stage_contract._ACTIVE_REQUEST_CONTRACT.set(contract)
        try:
            with patch.object(recovery.time, "sleep") as sleep:
                with self.assertRaises(recovery.NoWireProviderFailure):
                    stage_contract._provider_result(
                        "groq", "prompt", "model", contract, "api-key"
                    )
        finally:
            stage_contract._ACTIVE_REQUEST_CONTRACT.reset(token)

        self.assertEqual(calls, 1)
        sleep.assert_not_called()
        self.assertEqual(recovery._TERMINAL_RECOVERY_COUNT, 0)

    def test_non_temporal_append_capacity_is_not_made_retryable(self) -> None:
        calls = 0

        def quota_failure(provider, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise stage_contract.PlanningStageError(
                stage_contract.PlanningErrorCode.CAPACITY,
                "HTTP_429 status=429 quota exceeded",
                stage_id="planning.append_only_repair",
                provider=provider,
            )

        stage_contract._provider_result = quota_failure
        recovery._install_stage_temporal_capacity_bridge()
        contract = self._contract()
        token = stage_contract._ACTIVE_REQUEST_CONTRACT.set(contract)
        try:
            with patch.object(recovery.time, "sleep") as sleep:
                with self.assertRaises(stage_contract.PlanningStageError):
                    stage_contract._provider_result(
                        "groq", "prompt", "model", contract, "api-key"
                    )
        finally:
            stage_contract._ACTIVE_REQUEST_CONTRACT.reset(token)

        self.assertEqual(calls, 1)
        sleep.assert_not_called()
        self.assertEqual(recovery._TERMINAL_RECOVERY_COUNT, 0)


if __name__ == "__main__":
    unittest.main()

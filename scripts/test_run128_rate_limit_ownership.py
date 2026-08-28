from __future__ import annotations

import unittest
from unittest.mock import patch

from scripts import run124_terminal_provider_recovery as run124
from scripts import run125_cache_prefix_contract as prefix
from scripts import run125_capacity_routing_closure as closure


MODEL_20B = "openai/gpt-oss-20b"
MODEL_120B = "openai/gpt-oss-120b"
RUN128_TPM_ERROR = (
    "GROQ_HTTP_429 status=429 code=rate_limit_exceeded message=Rate limit reached "
    "for model `openai/gpt-oss-20b` on tokens per minute (TPM): Limit 8000"
)
TPD_ERROR = (
    "GROQ_HTTP_429 status=429 code=rate_limit_exceeded message=Rate limit reached "
    "on tokens per day (TPD): Limit 200000"
)


class Run128RateLimitOwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        router = closure.router
        self.original_index = closure._ACTIVE_GROQ_INDEX
        self.original_pool = closure._GROQ_MODEL_POOL
        self.original_installed = closure._INSTALLED
        self.original_is_model_unavailable = closure._is_model_unavailable
        self.original_groq_call = router._groq_call
        self.original_classify = router.classify_provider_failure
        self.original_retry_cap = router.RETRY_AFTER_MAX_SECONDS
        self.original_headers = dict(router._last_call_rate_limit_headers)
        self.had_marker = hasattr(router, "_ISCO_RUN128_RATE_LIMIT_OWNERSHIP")
        self.original_marker = getattr(router, "_ISCO_RUN128_RATE_LIMIT_OWNERSHIP", None)

        closure.capacity.reset_groq_capacity_state_for_tests()
        closure._GROQ_MODEL_POOL = prefix._PRODUCTION_GROQ_MODEL_POOL
        closure._ACTIVE_GROQ_INDEX = 0
        closure._INSTALLED = True
        router._last_call_rate_limit_headers.clear()
        if hasattr(router, "_ISCO_RUN128_RATE_LIMIT_OWNERSHIP"):
            delattr(router, "_ISCO_RUN128_RATE_LIMIT_OWNERSHIP")
        prefix._install_rate_limit_ownership()

    def tearDown(self) -> None:
        router = closure.router
        closure._ACTIVE_GROQ_INDEX = self.original_index
        closure._GROQ_MODEL_POOL = self.original_pool
        closure._INSTALLED = self.original_installed
        closure._is_model_unavailable = self.original_is_model_unavailable
        router._groq_call = self.original_groq_call
        router.classify_provider_failure = self.original_classify
        router.RETRY_AFTER_MAX_SECONDS = self.original_retry_cap
        router._last_call_rate_limit_headers.clear()
        router._last_call_rate_limit_headers.update(self.original_headers)
        if self.had_marker:
            router._ISCO_RUN128_RATE_LIMIT_OWNERSHIP = self.original_marker
        elif hasattr(router, "_ISCO_RUN128_RATE_LIMIT_OWNERSHIP"):
            delattr(router, "_ISCO_RUN128_RATE_LIMIT_OWNERSHIP")
        closure.capacity.reset_groq_capacity_state_for_tests()

    def test_run128_tpm_429_is_distinct_from_daily_quota(self) -> None:
        self.assertTrue(prefix._is_tpm_window_exhausted(RUN128_TPM_ERROR))
        self.assertFalse(prefix._is_tpm_window_exhausted(TPD_ERROR))

    def test_run128_tpm_429_is_model_failover_signal(self) -> None:
        self.assertTrue(closure._is_model_unavailable(RUN128_TPM_ERROR))
        self.assertTrue(closure._switch_groq_model("minute_token_window_exhausted"))
        self.assertEqual(closure._active_groq_model(), MODEL_120B)

    def test_retry_after_above_local_budget_never_becomes_partial_retry(self) -> None:
        closure.router.RETRY_AFTER_MAX_SECONDS = 20.0
        closure.router._last_call_rate_limit_headers["retry_after"] = "38"
        failure = closure.router.classify_provider_failure("groq", RUN128_TPM_ERROR)
        self.assertEqual(failure.telemetry_result, "retry_after_exceeds_budget")
        self.assertFalse(failure.open_circuit)

    def test_retry_after_inside_budget_remains_normal_bounded_retry(self) -> None:
        closure.router.RETRY_AFTER_MAX_SECONDS = 20.0
        closure.router._last_call_rate_limit_headers["retry_after"] = "18"
        failure = closure.router.classify_provider_failure("groq", RUN128_TPM_ERROR)
        self.assertEqual(failure.telemetry_result, "429")

    def test_final_model_preserves_full_provider_reset_for_run124(self) -> None:
        closure._ACTIVE_GROQ_INDEX = 1
        state = closure.capacity._model_state(MODEL_120B)
        state["contacted"] = True
        state["remaining_tokens"] = 4103
        state["reset_at_epoch"] = 1038.0
        closure.router._last_call_rate_limit_headers["retry_after"] = "38"

        with patch.object(closure.capacity.time, "time", return_value=1000.0):
            marker = prefix._terminal_tpm_window_error(RUN128_TPM_ERROR)

        text = str(marker)
        self.assertIn(f"model={MODEL_120B}", text)
        self.assertIn("remaining=4103", text)
        self.assertIn("reset_in=38.00s", text)
        self.assertIn("provider_evidence_failover_without_partial_retry", text)

        terminal = RuntimeError(
            "All free providers failed for planning subtask: "
            f"groq:{text} | openrouter:OPENROUTER_NO_PROVIDER_AVAILABLE"
        )
        self.assertEqual(run124._remaining_reset_seconds(terminal), 38.0)

    def test_terminal_reset_marker_is_not_retried_by_outer_provider_loop(self) -> None:
        marker = RuntimeError(
            f"GROQ_TPM_WINDOW_BUSY_PRECHECK model={MODEL_120B} "
            "remaining=4103 reset_in=38.00s action=provider_evidence_failover_without_partial_retry"
        )
        failure = closure.router.classify_provider_failure("groq", marker)
        self.assertEqual(failure.telemetry_result, "capacity_wait")
        self.assertFalse(failure.open_circuit)


if __name__ == "__main__":
    unittest.main()

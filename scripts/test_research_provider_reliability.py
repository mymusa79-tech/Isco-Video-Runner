from __future__ import annotations

import unittest
from unittest.mock import patch

from scripts import research_provider_reliability as rpr


class DailyQuotaDetectionTests(unittest.TestCase):
    def test_real_gemini_free_tier_quota_message_is_detected(self) -> None:
        # The exact text observed in the live production incident this fix closes.
        error = RuntimeError(
            "Error code: 429 - {'error': {'message': 'You exceeded your current quota ... "
            "Quota exceeded for metric: generativelanguage.googleapis.com/"
            "generate_content_free_tier_requests, limit: 20, model: gemini-3.7-flash"
            "\nPlease retry in 1.447794966s.', 'code': 'too_many_requests'}}"
        )
        self.assertTrue(rpr._is_daily_quota_failure(error))

    def test_plain_short_window_rate_limit_is_not_a_quota_failure(self) -> None:
        error = RuntimeError("HTTP 429 rate_limit_exceeded, try again shortly")
        self.assertFalse(rpr._is_daily_quota_failure(error))

    def test_unrelated_error_is_not_a_quota_failure(self) -> None:
        self.assertFalse(rpr._is_daily_quota_failure(RuntimeError("network reset by peer")))


class BackoffCalculationTests(unittest.TestCase):
    def test_uses_hinted_retry_after_when_present_and_bounded(self) -> None:
        error = RuntimeError("... Please retry in 1.447794966s.")
        with patch.object(rpr.random, "uniform", return_value=0.3):
            seconds = rpr._backoff_seconds(error, attempt=1)
        self.assertAlmostEqual(seconds, 1.447794966 + 0.3, places=6)

    def test_hint_above_ceiling_is_capped(self) -> None:
        error = RuntimeError("Please retry in 500s.")
        with patch.object(rpr.random, "uniform", return_value=0.0):
            seconds = rpr._backoff_seconds(error, attempt=1)
        self.assertEqual(seconds, rpr.MAX_RETRY_AFTER_SECONDS)

    def test_no_hint_falls_back_to_attempt_scaled_floor(self) -> None:
        error = RuntimeError("server error, try again")
        with patch.object(rpr.random, "uniform", return_value=0.0):
            seconds = rpr._backoff_seconds(error, attempt=2)
        self.assertEqual(seconds, rpr.MIN_BACKOFF_SECONDS * 2)


class GeminiResearchCallWithFallbackTests(unittest.TestCase):
    def test_success_on_first_gemini_attempt_never_sleeps_or_calls_fallback(self) -> None:
        with patch.object(rpr, "gemini_json_text", return_value={"items": []}) as gemini, \
                patch.object(rpr, "openrouter_json_text") as openrouter, \
                patch.object(rpr.time, "sleep") as sleep:
            result = rpr.gemini_research_call_with_fallback("key", "prompt", "model")
        self.assertEqual(result, {"items": []})
        gemini.assert_called_once()
        openrouter.assert_not_called()
        sleep.assert_not_called()

    def test_daily_quota_failure_skips_retry_and_uses_fallback_immediately(self) -> None:
        quota_error = RuntimeError(
            "Quota exceeded for metric: generate_content_free_tier_requests, limit: 20. "
            "Please retry in 1.4s."
        )
        with patch.object(rpr, "gemini_json_text", side_effect=quota_error) as gemini, \
                patch.object(rpr, "openrouter_json_text", return_value={"items": ["ok"]}) as openrouter, \
                patch.object(rpr.time, "sleep") as sleep:
            result = rpr.gemini_research_call_with_fallback("key", "prompt", "model")
        self.assertEqual(result, {"items": ["ok"]})
        gemini.assert_called_once()
        openrouter.assert_called_once()
        sleep.assert_not_called()

    def test_transient_rate_limit_retries_gemini_once_then_succeeds(self) -> None:
        transient_error = RuntimeError("HTTP 429 rate_limit_exceeded")
        with patch.object(
            rpr, "gemini_json_text", side_effect=[transient_error, {"items": ["ok"]}]
        ) as gemini, \
                patch.object(rpr, "openrouter_json_text") as openrouter, \
                patch.object(rpr.time, "sleep") as sleep:
            result = rpr.gemini_research_call_with_fallback("key", "prompt", "model")
        self.assertEqual(result, {"items": ["ok"]})
        self.assertEqual(gemini.call_count, 2)
        openrouter.assert_not_called()
        sleep.assert_called_once()

    def test_transient_rate_limit_exhausts_bounded_retry_then_falls_back(self) -> None:
        transient_error = RuntimeError("HTTP 429 rate_limit_exceeded")
        with patch.object(rpr, "gemini_json_text", side_effect=[transient_error, transient_error]) as gemini, \
                patch.object(rpr, "openrouter_json_text", return_value={"items": ["fallback"]}) as openrouter, \
                patch.object(rpr.time, "sleep") as sleep:
            result = rpr.gemini_research_call_with_fallback("key", "prompt", "model")
        self.assertEqual(result, {"items": ["fallback"]})
        self.assertEqual(gemini.call_count, rpr.MAX_GEMINI_ATTEMPTS_FOR_TRANSIENT_FAILURE)
        openrouter.assert_called_once()
        sleep.assert_called_once()

    def test_content_safety_block_never_falls_back(self) -> None:
        safety_error = RuntimeError("response blocked: prohibited_content")
        with patch.object(rpr, "gemini_json_text", side_effect=safety_error) as gemini, \
                patch.object(rpr, "openrouter_json_text") as openrouter, \
                patch.object(rpr.time, "sleep") as sleep:
            with self.assertRaises(RuntimeError) as ctx:
                rpr.gemini_research_call_with_fallback("key", "prompt", "model")
        self.assertIs(ctx.exception, safety_error)
        gemini.assert_called_once()
        openrouter.assert_not_called()
        sleep.assert_not_called()

    def test_non_retryable_non_safety_failure_falls_back_without_a_same_provider_retry(self) -> None:
        schema_error = RuntimeError("invalid json: schema mismatch")
        with patch.object(rpr, "gemini_json_text", side_effect=schema_error) as gemini, \
                patch.object(rpr, "openrouter_json_text", return_value={"items": ["fallback"]}) as openrouter, \
                patch.object(rpr.time, "sleep") as sleep:
            result = rpr.gemini_research_call_with_fallback("key", "prompt", "model")
        self.assertEqual(result, {"items": ["fallback"]})
        gemini.assert_called_once()
        openrouter.assert_called_once()
        sleep.assert_not_called()

    def test_both_providers_failing_raises_research_provider_exhausted_with_both_reasons(self) -> None:
        quota_error = RuntimeError("Quota exceeded for metric: generate_content_free_tier_requests")
        openrouter_error = RuntimeError("OpenRouter key unavailable")
        with patch.object(rpr, "gemini_json_text", side_effect=quota_error), \
                patch.object(rpr, "openrouter_json_text", side_effect=openrouter_error), \
                patch.object(rpr.time, "sleep"):
            with self.assertRaises(rpr.ResearchProviderExhausted) as ctx:
                rpr.gemini_research_call_with_fallback("key", "prompt", "model")
        message = str(ctx.exception)
        self.assertIn("Quota exceeded", message)
        self.assertIn("OpenRouter key unavailable", message)
        self.assertIs(ctx.exception.__cause__, openrouter_error)

    def test_all_providers_failing_never_touches_the_ai_budget_or_planning_modules(self) -> None:
        # Regression guard for the "do not touch Planning" constraint: this module
        # must not import task_level_planner_router, provider_capacity_hardening,
        # run_v3_voice, or anything AI-budget-related.
        import sys

        planning_markers = (
            "task_level_planner_router",
            "provider_capacity_hardening",
            "run_v3_voice",
            "ai_budget",
        )
        for name in sys.modules.get("scripts.research_provider_reliability").__dict__:
            self.assertFalse(
                any(marker in name for marker in planning_markers),
                f"research_provider_reliability leaked a Planning symbol: {name}",
            )


if __name__ == "__main__":
    unittest.main()

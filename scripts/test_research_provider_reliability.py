from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from scripts import research_provider_reliability as rpr


class _Response:
    def __init__(self, status_code: int, retry_after: str | None = None) -> None:
        self.status_code = status_code
        self.headers = {} if retry_after is None else {"Retry-After": retry_after}


class _HttpError(RuntimeError):
    def __init__(self, message: str, status_code: int, retry_after: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response = _Response(status_code, retry_after)


class DailyQuotaDetectionTests(unittest.TestCase):
    def test_real_gemini_free_tier_quota_message_is_detected(self) -> None:
        error = RuntimeError(
            "Error code: 429 - {'error': {'message': 'You exceeded your current quota ... "
            "Quota exceeded for metric: generativelanguage.googleapis.com/"
            "generate_content_free_tier_requests, limit: 20, model: gemini-3.7-flash"
            "\nPlease retry in 1.447794966s.', 'code': 'too_many_requests'}}"
        )
        self.assertTrue(rpr._is_daily_quota_failure(error))

    def test_plain_short_window_rate_limit_is_not_a_quota_failure(self) -> None:
        self.assertFalse(rpr._is_daily_quota_failure(RuntimeError("HTTP 429 rate_limit_exceeded")))

    def test_unrelated_error_is_not_a_quota_failure(self) -> None:
        self.assertFalse(rpr._is_daily_quota_failure(RuntimeError("network reset by peer")))


class SafeTelemetryExtractionTests(unittest.TestCase):
    def test_structured_http_status_and_retry_after_are_extracted_without_message(self) -> None:
        error = _HttpError("SECRET_PROMPT api-key-xyz", 429, "12.5")
        self.assertEqual(rpr._http_status(error), 429)
        self.assertEqual(rpr._parsed_retry_after_seconds(error), 12.5)

    def test_invalid_status_metadata_is_not_reported(self) -> None:
        error = _HttpError("opaque", 999)
        self.assertIsNone(rpr._http_status(error))


class BackoffCalculationTests(unittest.TestCase):
    def test_uses_hinted_retry_after_when_present_and_within_budget(self) -> None:
        error = RuntimeError("... Please retry in 1.447794966s.")
        with patch.object(rpr.random, "uniform", return_value=0.3):
            seconds = rpr._backoff_seconds(error, attempt=1)
        self.assertAlmostEqual(seconds, 1.447794966, places=6)

    def test_live_56s_hint_is_within_research_wait_budget(self) -> None:
        error = RuntimeError("Please retry in 56.7s.")
        with patch.object(rpr.random, "uniform", return_value=0.0):
            seconds = rpr._backoff_seconds(error, attempt=1)
        self.assertEqual(seconds, 56.7)

    def test_hint_above_wait_budget_requires_failover_not_partial_sleep(self) -> None:
        error = RuntimeError("Please retry in 500s.")
        with patch.object(rpr.random, "uniform", return_value=0.0):
            seconds = rpr._backoff_seconds(error, attempt=1)
        self.assertIsNone(seconds)

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

    def test_bounded_quota_retry_after_is_honored_once_then_succeeds(self) -> None:
        quota_error = RuntimeError(
            "Quota exceeded for metric: generate_content_free_tier_requests, limit: 20. "
            "Please retry in 1.4s."
        )
        with patch.object(rpr, "gemini_json_text", side_effect=[quota_error, {"items": ["ok"]}]) as gemini, \
                patch.object(rpr, "openrouter_json_text") as openrouter, \
                patch.object(rpr.time, "sleep") as sleep:
            result = rpr.gemini_research_call_with_fallback("key", "prompt", "model")
        self.assertEqual(result, {"items": ["ok"]})
        self.assertEqual(gemini.call_count, 2)
        openrouter.assert_not_called()
        sleep.assert_called_once_with(1.4)

    def test_quota_without_retry_after_fails_over_immediately(self) -> None:
        quota_error = RuntimeError(
            "Quota exceeded for metric: generate_content_free_tier_requests, limit: 20."
        )
        with patch.object(rpr, "gemini_json_text", side_effect=quota_error) as gemini, \
                patch.object(rpr, "openrouter_json_text", return_value={"items": ["ok"]}) as openrouter, \
                patch.object(rpr.time, "sleep") as sleep:
            result = rpr.gemini_research_call_with_fallback("key", "prompt", "model")
        self.assertEqual(result, {"items": ["ok"]})
        gemini.assert_called_once()
        openrouter.assert_called_once()
        sleep.assert_not_called()

    def test_live_49s_quota_then_invalid_json_recovers_via_schema_bound_free_router(self) -> None:
        live_error = RuntimeError(
            "Quota exceeded for metric: generate_content_free_tier_requests, limit: 20. "
            "Please retry in 49.3198s."
        )
        invalid_json = RuntimeError("OpenRouter returned invalid JSON")
        structured = {"candidates": [{"title": "ok"}]}
        with patch.object(rpr, "gemini_json_text", side_effect=[live_error, live_error]) as gemini, \
                patch.object(rpr.time, "sleep") as sleep, \
                patch.object(rpr, "openrouter_json_text", side_effect=[invalid_json, structured]) as openrouter:
            result = rpr.gemini_research_call_with_fallback("key", "prompt", "model")
        self.assertEqual(result, structured)
        self.assertEqual(gemini.call_count, 2)
        sleep.assert_called_once_with(49.3198)
        self.assertEqual(openrouter.call_count, 2)
        for call in openrouter.call_args_list:
            self.assertIs(call.kwargs["response_schema"], rpr.RESEARCH_RESPONSE_SCHEMA)
            self.assertEqual(call.kwargs["schema_name"], "isco_topic_research_response")

    def test_transient_rate_limit_retries_gemini_once_then_succeeds(self) -> None:
        transient_error = RuntimeError("HTTP 429 rate_limit_exceeded")
        with patch.object(
            rpr, "gemini_json_text", side_effect=[transient_error, {"items": ["ok"]}]
        ) as gemini, patch.object(rpr, "openrouter_json_text") as openrouter, patch.object(
            rpr.time, "sleep"
        ) as sleep:
            result = rpr.gemini_research_call_with_fallback("key", "prompt", "model")
        self.assertEqual(result, {"items": ["ok"]})
        self.assertEqual(gemini.call_count, 2)
        openrouter.assert_not_called()
        sleep.assert_called_once()

    def test_live_38s_retry_after_is_retried_not_forced_to_fallback(self) -> None:
        transient_error = RuntimeError("HTTP 429 rate_limit_exceeded. Please retry in 38s.")
        with patch.object(
            rpr, "gemini_json_text", side_effect=[transient_error, {"items": ["ok"]}]
        ) as gemini, patch.object(rpr, "openrouter_json_text") as openrouter, patch.object(
            rpr.time, "sleep"
        ) as sleep:
            result = rpr.gemini_research_call_with_fallback("key", "prompt", "model")
        self.assertEqual(result, {"items": ["ok"]})
        self.assertEqual(gemini.call_count, 2)
        openrouter.assert_not_called()
        sleep.assert_called_once_with(38.0)

    def test_retry_after_above_budget_skips_same_provider_retry_and_falls_back(self) -> None:
        transient_error = RuntimeError("HTTP 429 rate_limit_exceeded. Please retry in 90s.")
        with patch.object(rpr, "gemini_json_text", side_effect=transient_error) as gemini, \
                patch.object(rpr, "openrouter_json_text", return_value={"items": ["fallback"]}) as openrouter, \
                patch.object(rpr.time, "sleep") as sleep:
            result = rpr.gemini_research_call_with_fallback("key", "prompt", "model")
        self.assertEqual(result, {"items": ["fallback"]})
        gemini.assert_called_once()
        openrouter.assert_called_once()
        sleep.assert_not_called()

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

    def test_openrouter_is_schema_bound_from_first_fallback_and_retries_once_if_malformed(self) -> None:
        gemini_error = RuntimeError("HTTP 500 server error")
        invalid_json = RuntimeError("OpenRouter returned invalid JSON")
        structured = {"candidates": [{"title": "ok"}]}
        with patch.object(rpr, "gemini_json_text", side_effect=[gemini_error, gemini_error]), \
                patch.object(rpr.time, "sleep"), \
                patch.object(rpr, "openrouter_json_text", side_effect=[invalid_json, structured]) as openrouter:
            result = rpr.gemini_research_call_with_fallback("key", "prompt", "model")
        self.assertEqual(result, structured)
        self.assertEqual(openrouter.call_count, 2)
        first, second = openrouter.call_args_list
        for call in (first, second):
            self.assertEqual(call.kwargs["model"], "openrouter/free")
            self.assertEqual(call.kwargs["fallback_models"], ("openai/gpt-oss-20b:free",))
            self.assertIs(call.kwargs["response_schema"], rpr.RESEARCH_RESPONSE_SCHEMA)
            self.assertEqual(call.kwargs["schema_name"], "isco_topic_research_response")

    def test_structured_openrouter_retry_failure_is_bounded_and_sanitized(self) -> None:
        secret = "SECRET_RESEARCH_SENTINEL"
        gemini_error = RuntimeError("HTTP 429 rate_limit_exceeded")
        invalid_json = RuntimeError(f"OpenRouter returned invalid JSON {secret}")
        structured_error = _HttpError(f"provider unavailable {secret}", 503)
        output = io.StringIO()
        with patch.object(rpr, "gemini_json_text", side_effect=[gemini_error, gemini_error]), \
                patch.object(rpr.time, "sleep"), \
                patch.object(rpr, "openrouter_json_text", side_effect=[invalid_json, structured_error]) as openrouter, \
                redirect_stdout(output):
            with self.assertRaises(rpr.ResearchProviderExhausted) as ctx:
                rpr.gemini_research_call_with_fallback("secret-key", secret, "model")
        self.assertEqual(openrouter.call_count, 2)
        telemetry = output.getvalue()
        message = str(ctx.exception)
        self.assertIn("action=retry_structured", telemetry)
        self.assertIn("attempt=2 action=exhausted", telemetry)
        self.assertIn("openrouter_first_failure_class=invalid_json", message)
        self.assertNotIn(secret, telemetry + message)
        self.assertIs(ctx.exception.__cause__, structured_error)

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

    def test_non_retryable_non_safety_failure_falls_back_without_same_provider_retry(self) -> None:
        schema_error = RuntimeError("invalid json: schema mismatch")
        with patch.object(rpr, "gemini_json_text", side_effect=schema_error) as gemini, \
                patch.object(rpr, "openrouter_json_text", return_value={"items": ["fallback"]}) as openrouter, \
                patch.object(rpr.time, "sleep") as sleep:
            result = rpr.gemini_research_call_with_fallback("key", "prompt", "model")
        self.assertEqual(result, {"items": ["fallback"]})
        gemini.assert_called_once()
        openrouter.assert_called_once()
        sleep.assert_not_called()

    def test_both_providers_failure_is_sanitized_and_reports_normalized_classes(self) -> None:
        secret_prompt = "TOP_SECRET_PROMPT_SENTINEL"
        secret_key = "sk-secret-sentinel"
        gemini_error = _HttpError(
            f"Quota exceeded request={secret_prompt} key={secret_key}", 429, "1.4"
        )
        openrouter_error = _HttpError(
            f"Unauthorized request={secret_prompt} key={secret_key}", 401
        )
        output = io.StringIO()
        with patch.object(rpr, "gemini_json_text", side_effect=gemini_error), patch.object(
            rpr, "openrouter_json_text", side_effect=openrouter_error
        ), patch.object(rpr.time, "sleep"), redirect_stdout(output):
            with self.assertRaises(rpr.ResearchProviderExhausted) as ctx:
                rpr.gemini_research_call_with_fallback(secret_key, secret_prompt, "model")

        telemetry = output.getvalue()
        message = str(ctx.exception)
        combined = telemetry + message
        self.assertIn("provider=gemini", telemetry)
        self.assertIn("failure_class=429", telemetry)
        self.assertIn("http_status=429", telemetry)
        self.assertIn("retry_after_s=1.4", telemetry)
        self.assertIn("provider=openrouter", telemetry)
        self.assertIn("failure_class=auth_error", telemetry)
        self.assertIn("http_status=401", telemetry)
        self.assertIn("action=exhausted", telemetry)
        self.assertIn("gemini_failure_class=429", message)
        self.assertIn("openrouter_failure_class=auth_error", message)
        self.assertNotIn(secret_prompt, combined)
        self.assertNotIn(secret_key, combined)
        self.assertNotIn("Unauthorized request", combined)
        self.assertNotIn("Quota exceeded request", combined)
        self.assertIs(ctx.exception.__cause__, openrouter_error)

    def test_retry_telemetry_contains_only_safe_decision_metadata(self) -> None:
        secret = "DO_NOT_LOG_THIS_SECRET"
        transient_error = _HttpError(f"timeout {secret}", 504, "2")
        output = io.StringIO()
        with patch.object(
            rpr, "gemini_json_text", side_effect=[transient_error, {"items": ["ok"]}]
        ), patch.object(rpr, "openrouter_json_text"), patch.object(
            rpr.time, "sleep"
        ), redirect_stdout(output):
            result = rpr.gemini_research_call_with_fallback("secret-key", secret, "model")
        self.assertEqual(result, {"items": ["ok"]})
        telemetry = output.getvalue()
        self.assertIn("RESEARCH_PROVIDER_TELEMETRY", telemetry)
        self.assertIn("provider=gemini", telemetry)
        self.assertIn("failure_class=timeout", telemetry)
        self.assertIn("http_status=504", telemetry)
        self.assertIn("retry_after_s=2", telemetry)
        self.assertIn("action=retry", telemetry)
        self.assertNotIn(secret, telemetry)
        self.assertNotIn("secret-key", telemetry)

    def test_all_providers_failing_never_touches_ai_budget_or_planning_modules(self) -> None:
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

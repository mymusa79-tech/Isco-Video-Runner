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


class QuotaAndBackoffTests(unittest.TestCase):
    def test_free_tier_quota_marker_is_detected(self) -> None:
        error = RuntimeError(
            "Quota exceeded for metric generate_content_free_tier_requests. Please retry in 41.6s."
        )
        self.assertTrue(rpr._is_daily_quota_failure(error))

    def test_retry_after_up_to_one_minute_is_honored(self) -> None:
        error = RuntimeError("HTTP 429. Please retry in 56.7s.")
        with patch.object(rpr.random, "uniform", return_value=0.0):
            self.assertEqual(rpr._backoff_seconds(error, 1), 56.7)

    def test_retry_after_above_one_minute_fails_over(self) -> None:
        error = RuntimeError("HTTP 429. Please retry in 90s.")
        with patch.object(rpr.random, "uniform", return_value=0.0):
            self.assertIsNone(rpr._backoff_seconds(error, 1))

    def test_quota_without_bounded_retry_hint_does_not_spin(self) -> None:
        error = RuntimeError("quota exceeded for generate_content_free_tier_requests")
        classification = rpr.classify_provider_failure("gemini", error)
        self.assertFalse(rpr._same_provider_retry_allowed(error, classification, 1))


class GeminiResearchCallWithFallbackTests(unittest.TestCase):
    def test_success_on_first_gemini_attempt_never_calls_fallback(self) -> None:
        with patch.object(rpr, "gemini_json_text", return_value={"items": []}) as gemini, patch.object(rpr, "openrouter_json_text") as openrouter, patch.object(rpr.time, "sleep") as sleep:
            result = rpr.gemini_research_call_with_fallback("key", "prompt", "model")
        self.assertEqual(result, {"items": []})
        gemini.assert_called_once()
        openrouter.assert_not_called()
        sleep.assert_not_called()

    def test_live_retry_after_quota_is_honored_once_then_succeeds(self) -> None:
        quota = RuntimeError(
            "Quota exceeded for metric generate_content_free_tier_requests. Please retry in 41.6s."
        )
        with patch.object(rpr, "gemini_json_text", side_effect=[quota, {"items": ["ok"]}]) as gemini, patch.object(rpr, "openrouter_json_text") as openrouter, patch.object(rpr.time, "sleep") as sleep:
            result = rpr.gemini_research_call_with_fallback("key", "prompt", "model")
        self.assertEqual(result, {"items": ["ok"]})
        self.assertEqual(gemini.call_count, 2)
        sleep.assert_called_once_with(41.6)
        openrouter.assert_not_called()

    def test_retry_after_above_budget_fails_over_without_partial_sleep(self) -> None:
        quota = RuntimeError(
            "Quota exceeded for metric generate_content_free_tier_requests. Please retry in 90s."
        )
        with patch.object(rpr, "gemini_json_text", side_effect=quota) as gemini, patch.object(rpr, "openrouter_json_text", return_value={"items": ["fallback"]}) as openrouter, patch.object(rpr.time, "sleep") as sleep:
            result = rpr.gemini_research_call_with_fallback("key", "prompt", "model")
        self.assertEqual(result, {"items": ["fallback"]})
        gemini.assert_called_once()
        openrouter.assert_called_once()
        sleep.assert_not_called()

    def test_quota_without_retry_after_fails_over_immediately(self) -> None:
        quota = RuntimeError("Quota exceeded for metric generate_content_free_tier_requests")
        with patch.object(rpr, "gemini_json_text", side_effect=quota) as gemini, patch.object(rpr, "openrouter_json_text", return_value={"items": ["fallback"]}) as openrouter, patch.object(rpr.time, "sleep") as sleep:
            result = rpr.gemini_research_call_with_fallback("key", "prompt", "model")
        self.assertEqual(result, {"items": ["fallback"]})
        gemini.assert_called_once()
        openrouter.assert_called_once()
        sleep.assert_not_called()

    def test_transient_rate_limit_retries_once_then_succeeds(self) -> None:
        transient = RuntimeError("HTTP 429 rate_limit_exceeded")
        with patch.object(rpr, "gemini_json_text", side_effect=[transient, {"items": ["ok"]}]) as gemini, patch.object(rpr, "openrouter_json_text") as openrouter, patch.object(rpr.time, "sleep") as sleep:
            result = rpr.gemini_research_call_with_fallback("key", "prompt", "model")
        self.assertEqual(result, {"items": ["ok"]})
        self.assertEqual(gemini.call_count, 2)
        sleep.assert_called_once()
        openrouter.assert_not_called()

    def test_openrouter_invalid_json_retries_via_free_router_with_schema(self) -> None:
        gemini_error = RuntimeError("HTTP 500 server error")
        invalid_json = RuntimeError("OpenRouter returned invalid JSON")
        structured = {"candidates": [{"title": "ok"}]}
        with patch.object(rpr, "gemini_json_text", side_effect=[gemini_error, gemini_error]), patch.object(rpr.time, "sleep"), patch.object(rpr, "openrouter_json_text", side_effect=[invalid_json, structured]) as openrouter:
            result = rpr.gemini_research_call_with_fallback("key", "prompt", "model")
        self.assertEqual(result, structured)
        self.assertEqual(openrouter.call_count, 2)
        first, second = openrouter.call_args_list
        self.assertEqual(first.kwargs["model"], "openrouter/free")
        self.assertEqual(second.kwargs["model"], "openrouter/free")
        self.assertEqual(second.kwargs["fallback_models"], ("openai/gpt-oss-20b:free",))
        self.assertIs(second.kwargs["response_schema"], rpr.RESEARCH_RESPONSE_SCHEMA)
        self.assertEqual(second.kwargs["schema_name"], "isco_topic_research_response")

    def test_structured_retry_failure_is_bounded_and_sanitized(self) -> None:
        secret = "SECRET_RESEARCH_SENTINEL"
        gemini_error = RuntimeError("HTTP 429 rate_limit_exceeded")
        invalid_json = RuntimeError(f"OpenRouter returned invalid JSON {secret}")
        structured_error = _HttpError(f"provider unavailable {secret}", 503)
        output = io.StringIO()
        with patch.object(rpr, "gemini_json_text", side_effect=[gemini_error, gemini_error]), patch.object(rpr.time, "sleep"), patch.object(rpr, "openrouter_json_text", side_effect=[invalid_json, structured_error]) as openrouter, redirect_stdout(output):
            with self.assertRaises(rpr.ResearchProviderExhausted) as ctx:
                rpr.gemini_research_call_with_fallback("secret-key", secret, "model")
        self.assertEqual(openrouter.call_count, 2)
        combined = output.getvalue() + str(ctx.exception)
        self.assertIn("action=retry_structured", combined)
        self.assertIn("attempt=2 action=exhausted", combined)
        self.assertNotIn(secret, combined)
        self.assertIs(ctx.exception.__cause__, structured_error)

    def test_content_safety_block_never_falls_back(self) -> None:
        safety_error = RuntimeError("response blocked: prohibited_content")
        with patch.object(rpr, "gemini_json_text", side_effect=safety_error) as gemini, patch.object(rpr, "openrouter_json_text") as openrouter, patch.object(rpr.time, "sleep") as sleep:
            with self.assertRaises(RuntimeError) as ctx:
                rpr.gemini_research_call_with_fallback("key", "prompt", "model")
        self.assertIs(ctx.exception, safety_error)
        gemini.assert_called_once()
        openrouter.assert_not_called()
        sleep.assert_not_called()

    def test_telemetry_never_leaks_prompt_or_key(self) -> None:
        secret = "DO_NOT_LOG_THIS_SECRET"
        transient = _HttpError(f"timeout {secret}", 504, "2")
        output = io.StringIO()
        with patch.object(rpr, "gemini_json_text", side_effect=[transient, {"items": ["ok"]}]), patch.object(rpr, "openrouter_json_text"), patch.object(rpr.time, "sleep"), redirect_stdout(output):
            result = rpr.gemini_research_call_with_fallback("secret-key", secret, "model")
        self.assertEqual(result, {"items": ["ok"]})
        telemetry = output.getvalue()
        self.assertIn("failure_class=timeout", telemetry)
        self.assertIn("retry_after_s=2", telemetry)
        self.assertNotIn(secret, telemetry)
        self.assertNotIn("secret-key", telemetry)


if __name__ == "__main__":
    unittest.main()

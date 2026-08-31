from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from provider_failure import classify_provider_failure  # noqa: E402

from isco_video_agent.ai_budget import AttemptOutcome  # noqa: E402


class ProviderFailureTaxonomyTests(unittest.TestCase):
    def test_rate_limit_maps_consistently_and_opens_circuit(self) -> None:
        failure = classify_provider_failure("gemini", RuntimeError("HTTP 429 rate limited"))
        self.assertEqual(failure.telemetry_result, "429")
        self.assertEqual(failure.budget_outcome, AttemptOutcome.RATE_LIMITED)
        self.assertTrue(failure.open_circuit)

    def test_openrouter_local_circuit_marker_is_distinct_and_session_permanent(self) -> None:
        """run125_capacity_routing_closure.py's own preflight/structural block marker."""
        failure = classify_provider_failure(
            "openrouter",
            RuntimeError(
                "OPENROUTER_UNAVAILABLE_THIS_RUN reason=preflight_blocked: "
                "openrouter readiness blocked: key spend capacity exhausted"
            ),
        )
        self.assertEqual(failure.telemetry_result, "capacity_unavailable")
        self.assertEqual(failure.budget_outcome, AttemptOutcome.OTHER)
        self.assertTrue(failure.open_circuit)

    def test_openrouter_real_api_no_provider_response_is_also_capacity_unavailable(self) -> None:
        """A genuine OpenRouter completions-endpoint error, formatted the way
        _safe_api_error() actually formats real HTTP failures - this must be
        recognized on its own wording, not only via the Runner's local marker above."""
        failure = classify_provider_failure(
            "openrouter",
            RuntimeError(
                "OPENROUTER_HTTP_404 status=404 code=no_endpoints_found "
                "message=No endpoints found matching your data policy"
            ),
        )
        self.assertEqual(failure.telemetry_result, "capacity_unavailable")
        self.assertEqual(failure.budget_outcome, AttemptOutcome.OTHER)
        self.assertTrue(failure.open_circuit)

    def test_openrouter_model_not_found_is_configuration_failure(self) -> None:
        failure = classify_provider_failure(
            "openrouter",
            RuntimeError("OPENROUTER_MODEL_NOT_FOUND status=404 message=Model missing not found"),
        )
        self.assertEqual(failure.telemetry_result, "model_not_found")
        self.assertEqual(failure.budget_outcome, AttemptOutcome.OTHER)
        self.assertTrue(failure.open_circuit)

    def test_groq_413_is_request_specific_not_session_permanent(self) -> None:
        failure = classify_provider_failure("groq", RuntimeError("Groq HTTP 413"))
        self.assertEqual(failure.telemetry_result, "payload_too_large")
        self.assertEqual(failure.budget_outcome, AttemptOutcome.OTHER)
        self.assertFalse(failure.open_circuit)

    def test_non_groq_413_is_also_request_specific(self) -> None:
        failure = classify_provider_failure(
            "openrouter", RuntimeError("HTTP 413 payload too large")
        )
        self.assertEqual(failure.telemetry_result, "payload_too_large")
        self.assertEqual(failure.budget_outcome, AttemptOutcome.OTHER)
        self.assertFalse(failure.open_circuit)

    def test_auth_and_bad_request_are_session_permanent(self) -> None:
        cases = [
            ("HTTP 401 Unauthorized", "auth_error"),
            ("HTTP 403 Forbidden", "auth_error"),
            ("HTTP 400 Bad Request", "bad_request"),
            ("invalid argument: response_format", "bad_request"),
        ]
        for detail, result in cases:
            with self.subTest(detail=detail):
                failure = classify_provider_failure("gemini", RuntimeError(detail))
                self.assertEqual(failure.telemetry_result, result)
                self.assertEqual(failure.budget_outcome, AttemptOutcome.OTHER)
                self.assertTrue(failure.open_circuit)

    def test_schema_truncation_timeout_and_network_have_one_mapping(self) -> None:
        cases = [
            ("Provider returned invalid JSON", "invalid_json", AttemptOutcome.SCHEMA_INVALID),
            ("GEMINI_EMPTY_OUTPUT", "premature_response", AttemptOutcome.TRUNCATED),
            ("MALFORMED_FUNCTION_CALL", "invalid_json", AttemptOutcome.SCHEMA_INVALID),
            ("premature response", "premature_response", AttemptOutcome.TRUNCATED),
            (
                "GEMINI_INTERACTION_INCOMPLETE_MAX_TOKENS",
                "premature_response",
                AttemptOutcome.TRUNCATED,
            ),
            ("request timed out", "timeout", AttemptOutcome.TIMEOUT),
            ("network connection reset", "network_error", AttemptOutcome.NETWORK_ERROR),
        ]
        for detail, telemetry_result, budget_outcome in cases:
            with self.subTest(detail=detail):
                failure = classify_provider_failure("gemini", RuntimeError(detail))
                self.assertEqual(failure.telemetry_result, telemetry_result)
                self.assertEqual(failure.budget_outcome, budget_outcome)
                self.assertFalse(failure.open_circuit)

    def test_provider_semantic_blocks_are_not_technical_failures(self) -> None:
        for detail in (
            "SAFETY",
            "RECITATION",
            "BLOCKLIST",
            "PROHIBITED_CONTENT",
            "SPII",
            "MODEL_ARMOR",
        ):
            with self.subTest(detail=detail):
                failure = classify_provider_failure("gemini", RuntimeError(detail))
                self.assertEqual(failure.telemetry_result, "content_blocked")
                self.assertEqual(failure.budget_outcome, AttemptOutcome.CONTENT_BLOCKED)
                self.assertFalse(failure.open_circuit)

    def test_server_errors_are_explicit_but_not_session_permanent(self) -> None:
        failure = classify_provider_failure("gemini", RuntimeError("HTTP 503 server error"))
        self.assertEqual(failure.telemetry_result, "server_error")
        self.assertEqual(failure.budget_outcome, AttemptOutcome.OTHER)
        self.assertFalse(failure.open_circuit)

    def test_unknown_failure_is_other_and_non_permanent(self) -> None:
        failure = classify_provider_failure("groq", RuntimeError("unexpected provider failure"))
        self.assertEqual(failure.telemetry_result, "other")
        self.assertEqual(failure.budget_outcome, AttemptOutcome.OTHER)
        self.assertFalse(failure.open_circuit)


if __name__ == "__main__":
    unittest.main()

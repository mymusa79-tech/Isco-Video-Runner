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

    def test_groq_413_is_explicit_and_session_permanent(self) -> None:
        failure = classify_provider_failure("groq", RuntimeError("Groq HTTP 413"))
        self.assertEqual(failure.telemetry_result, "payload_too_large")
        self.assertEqual(failure.budget_outcome, AttemptOutcome.OTHER)
        self.assertTrue(failure.open_circuit)

    def test_non_groq_413_does_not_expand_circuit_policy(self) -> None:
        failure = classify_provider_failure("openrouter-free-router", RuntimeError("HTTP 413 payload too large"))
        self.assertEqual(failure.telemetry_result, "payload_too_large")
        self.assertEqual(failure.budget_outcome, AttemptOutcome.OTHER)
        self.assertFalse(failure.open_circuit)

    def test_schema_truncation_timeout_and_network_have_one_mapping(self) -> None:
        cases = [
            ("Provider returned invalid JSON", "invalid_json", AttemptOutcome.SCHEMA_INVALID),
            ("premature response", "premature_response", AttemptOutcome.TRUNCATED),
            ("request timed out", "timeout", AttemptOutcome.TIMEOUT),
            ("network connection reset", "network_error", AttemptOutcome.NETWORK_ERROR),
        ]
        for detail, telemetry_result, budget_outcome in cases:
            with self.subTest(detail=detail):
                failure = classify_provider_failure("gemini", RuntimeError(detail))
                self.assertEqual(failure.telemetry_result, telemetry_result)
                self.assertEqual(failure.budget_outcome, budget_outcome)
                self.assertFalse(failure.open_circuit)

    def test_unknown_failure_is_other_and_non_permanent(self) -> None:
        failure = classify_provider_failure("groq", RuntimeError("unexpected provider failure"))
        self.assertEqual(failure.telemetry_result, "other")
        self.assertEqual(failure.budget_outcome, AttemptOutcome.OTHER)
        self.assertFalse(failure.open_circuit)


if __name__ == "__main__":
    unittest.main()

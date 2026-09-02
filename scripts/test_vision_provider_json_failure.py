from __future__ import annotations

import unittest

from isco_video_agent.ai_budget import AttemptOutcome
from scripts import vision_provider_reliability as mesh


class VisionProviderJsonFailureTests(unittest.TestCase):
    def test_gemini_json_failures_are_retryable_schema_failures(self) -> None:
        cases = (
            ValueError("Gemini returned an empty JSON response"),
            ValueError("Gemini did not return a complete JSON object"),
            ValueError("Gemini JSON response must be an object"),
        )
        for exc in cases:
            with self.subTest(message=str(exc)):
                self.assertTrue(mesh._is_retryable_provider_failure(exc))
                self.assertIs(mesh._attempt_outcome(exc), AttemptOutcome.SCHEMA_INVALID)

    def test_auth_failure_remains_fail_loud_not_retryable(self) -> None:
        exc = RuntimeError("401 unauthorized")
        self.assertFalse(mesh._is_retryable_provider_failure(exc))
        self.assertIs(mesh._attempt_outcome(exc), AttemptOutcome.OTHER)


if __name__ == "__main__":
    unittest.main()

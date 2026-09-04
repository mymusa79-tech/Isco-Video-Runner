from __future__ import annotations

import unittest
from unittest.mock import patch

from scripts import research_provider_reliability as rpr


class ResearchProviderRetrySequenceTests(unittest.TestCase):
    def test_timeout_then_live_retry_after_gets_one_provider_directed_retry(self) -> None:
        timeout = RuntimeError("request timed out")
        quota = RuntimeError(
            "Quota exceeded for metric generate_content_free_tier_requests. Please retry in 56.7s."
        )
        with patch.object(
            rpr, "gemini_json_text", side_effect=[timeout, quota, {"candidates": [{"title": "ok"}]}]
        ) as gemini, patch.object(
            rpr, "openrouter_json_text"
        ) as openrouter, patch.object(
            rpr.random, "uniform", return_value=0.0
        ), patch.object(
            rpr.time, "sleep"
        ) as sleep:
            result = rpr.gemini_research_call_with_fallback("key", "prompt", "model")

        self.assertEqual(result, {"candidates": [{"title": "ok"}]})
        self.assertEqual(gemini.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1.0, 56.7])
        openrouter.assert_not_called()

    def test_provider_directed_retry_is_never_honored_twice(self) -> None:
        first = RuntimeError(
            "Quota exceeded for metric generate_content_free_tier_requests. Please retry in 1.4s."
        )
        second = RuntimeError(
            "Quota exceeded for metric generate_content_free_tier_requests. Please retry in 2.0s."
        )
        with patch.object(
            rpr, "gemini_json_text", side_effect=[first, second]
        ) as gemini, patch.object(
            rpr, "openrouter_json_text", return_value={"candidates": [{"title": "fallback"}]}
        ) as openrouter, patch.object(
            rpr.time, "sleep"
        ) as sleep:
            result = rpr.gemini_research_call_with_fallback("key", "prompt", "model")

        self.assertEqual(result, {"candidates": [{"title": "fallback"}]})
        self.assertEqual(gemini.call_count, 2)
        sleep.assert_called_once_with(1.4)
        openrouter.assert_called_once()


if __name__ == "__main__":
    unittest.main()

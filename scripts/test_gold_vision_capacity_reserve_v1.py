from __future__ import annotations

import unittest
from unittest.mock import patch

from scripts import gold_final_critic_text_fallback as gold_fallback
from scripts import gold_vision_capacity_reserve_v1 as reserve


class GoldVisionCapacityReserveV1Tests(unittest.TestCase):
    def test_reserve_is_bounded_and_scales_with_request(self) -> None:
        self.assertEqual(reserve._gold_reserve_tokens(1000), reserve.GOLD_RESERVE_MIN_TOKENS)
        self.assertEqual(reserve._gold_reserve_tokens(10000), reserve.GOLD_RESERVE_MAX_TOKENS)
        mid = reserve._gold_reserve_tokens(4142)
        self.assertGreaterEqual(mid, 3200)
        self.assertLessEqual(mid, 4200)

    def test_last_live_provider_requires_both_alternates_unavailable(self) -> None:
        with patch.object(reserve, "_gemini_unavailable", return_value=True), patch.object(
            reserve, "_openrouter_unavailable", return_value=True
        ):
            self.assertTrue(reserve._groq_is_last_live_vision_provider())
        with patch.object(reserve, "_gemini_unavailable", return_value=False), patch.object(
            reserve, "_openrouter_unavailable", return_value=True
        ):
            self.assertFalse(reserve._groq_is_last_live_vision_provider())

    def test_installer_expands_gold_physical_vision_budget_to_five(self) -> None:
        before_vision = gold_fallback._FINAL_CRITIC_VISION_MAX_PROVIDER_ATTEMPTS
        before_total = gold_fallback._FINAL_CRITIC_TOTAL_PROVIDER_ATTEMPTS
        try:
            reserve._expand_truthful_gold_attempt_budget()
            self.assertGreaterEqual(gold_fallback._FINAL_CRITIC_VISION_MAX_PROVIDER_ATTEMPTS, 5)
            self.assertEqual(
                gold_fallback._FINAL_CRITIC_TOTAL_PROVIDER_ATTEMPTS,
                gold_fallback._FINAL_CRITIC_VISION_MAX_PROVIDER_ATTEMPTS
                + gold_fallback._FINAL_CRITIC_TEXT_MAX_PROVIDER_ATTEMPTS,
            )
        finally:
            gold_fallback._FINAL_CRITIC_VISION_MAX_PROVIDER_ATTEMPTS = before_vision
            gold_fallback._FINAL_CRITIC_TOTAL_PROVIDER_ATTEMPTS = before_total


if __name__ == "__main__":
    unittest.main()

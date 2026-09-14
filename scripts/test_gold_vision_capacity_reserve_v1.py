from __future__ import annotations

import unittest
from unittest.mock import patch

from scripts import gold_final_critic_text_fallback as gold_fallback
from scripts import gold_vision_capacity_reserve_v1 as reserve


class GoldVisionCapacityReserveV1Tests(unittest.TestCase):
    def test_priority_margin_is_bounded_and_scales_with_request(self) -> None:
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

    def test_gold_physical_retry_budget_is_scoped_and_restored(self) -> None:
        before_vision = gold_fallback._FINAL_CRITIC_VISION_MAX_PROVIDER_ATTEMPTS
        before_total = gold_fallback._FINAL_CRITIC_TOTAL_PROVIDER_ATTEMPTS
        with reserve._scoped_gold_attempt_budget():
            self.assertGreaterEqual(
                gold_fallback._FINAL_CRITIC_VISION_MAX_PROVIDER_ATTEMPTS,
                reserve.GOLD_VISION_PHYSICAL_ATTEMPT_CAP,
            )
            self.assertEqual(
                gold_fallback._FINAL_CRITIC_TOTAL_PROVIDER_ATTEMPTS,
                gold_fallback._FINAL_CRITIC_VISION_MAX_PROVIDER_ATTEMPTS
                + gold_fallback._FINAL_CRITIC_TEXT_MAX_PROVIDER_ATTEMPTS,
            )
        self.assertEqual(gold_fallback._FINAL_CRITIC_VISION_MAX_PROVIDER_ATTEMPTS, before_vision)
        self.assertEqual(gold_fallback._FINAL_CRITIC_TOTAL_PROVIDER_ATTEMPTS, before_total)

    def test_gold_admission_applies_cushion_before_wire(self) -> None:
        original = reserve.capacity._admit_groq
        captured: dict[str, int] = {}

        def admit(estimated_tokens: int, *, reserve_tokens: int = 0) -> float:
            captured["estimated_tokens"] = estimated_tokens
            captured["reserve_tokens"] = reserve_tokens
            return 0.0

        try:
            reserve.capacity._admit_groq = admit
            reserve._install_reserve_admission()
            token = reserve._GOLD_ACTIVE.set(True)
            try:
                reserve.capacity._admit_groq(4113)
            finally:
                reserve._GOLD_ACTIVE.reset(token)
        finally:
            reserve.capacity._admit_groq = original

        self.assertEqual(captured["estimated_tokens"], 4113)
        self.assertEqual(captured["reserve_tokens"], reserve._gold_reserve_tokens(4113))

    def test_explicit_gold_retry_cushion_is_forwarded_once_not_added_twice(self) -> None:
        original = reserve.capacity._admit_groq
        captured: dict[str, int] = {}

        def admit(estimated_tokens: int, *, reserve_tokens: int = 0) -> float:
            captured["estimated_tokens"] = estimated_tokens
            captured["reserve_tokens"] = reserve_tokens
            return 0.0

        try:
            reserve.capacity._admit_groq = admit
            reserve._install_reserve_admission()
            token = reserve._GOLD_ACTIVE.set(True)
            try:
                explicit = reserve._gold_reserve_tokens(4113)
                reserve.capacity._admit_groq(4113, reserve_tokens=explicit)
            finally:
                reserve._GOLD_ACTIVE.reset(token)
        finally:
            reserve.capacity._admit_groq = original

        self.assertEqual(captured["estimated_tokens"], 4113)
        self.assertEqual(captured["reserve_tokens"], reserve._gold_reserve_tokens(4113))

    def test_gold_groq_cooldown_reports_wait_then_returns_to_vision(self) -> None:
        original = reserve.vision_mesh._run_groq_attempt
        transient = reserve.vision_contract.VisionStageError(
            reserve.vision_contract.VisionErrorCode.PROVIDER_TRANSIENT,
            "HTTP 429 rate limit",
            provider="groq",
        )
        calls = {"count": 0}

        def wire(*_args, **_kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                raise transient
            return {"status": "pass"}

        try:
            reserve.vision_mesh._run_groq_attempt = wire
            active_token = reserve._GOLD_ACTIVE.set(True)
            retry_token = reserve._GOLD_GROQ_RETRY_SPENT.set(False)
            try:
                with patch.object(
                    reserve.shared_capacity,
                    "groq_capacity_snapshot",
                    return_value={"last_estimated_tokens": 4113, "blocked_reason": None},
                ), patch.object(
                    reserve.shared_capacity,
                    "groq_capacity_pacing_decision",
                    return_value={
                        "action": "wait",
                        "reason": "remaining_below_admission_threshold",
                        "wait_until_epoch": 12.0,
                    },
                ), patch.object(
                    reserve.capacity,
                    "_admit_groq",
                    return_value=2.0,
                ) as admit, patch.object(
                    reserve, "update_stage"
                ) as progress:
                    reserve._install_gold_groq_retry()
                    result = reserve.vision_mesh._run_groq_attempt()
            finally:
                reserve._GOLD_GROQ_RETRY_SPENT.reset(retry_token)
                reserve._GOLD_ACTIVE.reset(active_token)

            self.assertEqual(result, {"status": "pass"})
            admit.assert_called_once_with(
                4113,
                reserve_tokens=reserve._gold_reserve_tokens(4113),
            )
            self.assertEqual(
                [call.args[0] for call in progress.call_args_list],
                ["provider_wait", "gold_vision"],
            )
        finally:
            reserve.vision_mesh._run_groq_attempt = original

    def test_gold_groq_retry_is_spent_once_across_repeated_mesh_invocations(self) -> None:
        original = reserve.vision_mesh._run_groq_attempt
        transient = reserve.vision_contract.VisionStageError(
            reserve.vision_contract.VisionErrorCode.PROVIDER_TRANSIENT,
            "HTTP 429 rate limit",
            provider="groq",
        )
        calls = {"count": 0}

        def wire(*_args, **_kwargs):
            calls["count"] += 1
            raise transient

        try:
            reserve.vision_mesh._run_groq_attempt = wire
            active_token = reserve._GOLD_ACTIVE.set(True)
            retry_token = reserve._GOLD_GROQ_RETRY_SPENT.set(False)
            try:
                with patch.object(
                    reserve.shared_capacity,
                    "groq_capacity_snapshot",
                    return_value={"last_estimated_tokens": 4113, "blocked_reason": None},
                ), patch.object(
                    reserve.shared_capacity,
                    "groq_capacity_pacing_decision",
                    return_value={"action": "wait", "reason": "rate", "wait_until_epoch": 12.0},
                ), patch.object(
                    reserve.capacity,
                    "_admit_groq",
                    return_value=2.0,
                ) as admit, patch.object(reserve, "update_stage"):
                    reserve._install_gold_groq_retry()
                    with self.assertRaises(reserve.vision_contract.VisionStageError):
                        reserve.vision_mesh._run_groq_attempt()
                    with self.assertRaises(reserve.vision_contract.VisionStageError):
                        reserve.vision_mesh._run_groq_attempt()
            finally:
                reserve._GOLD_GROQ_RETRY_SPENT.reset(retry_token)
                reserve._GOLD_ACTIVE.reset(active_token)

            self.assertEqual(calls["count"], 3)
            self.assertEqual(admit.call_count, 1)
        finally:
            reserve.vision_mesh._run_groq_attempt = original


if __name__ == "__main__":
    unittest.main()

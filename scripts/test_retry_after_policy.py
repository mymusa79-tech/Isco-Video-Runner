from __future__ import annotations

import unittest

from scripts.retry_after_policy import parse_retry_after_seconds, retry_delay_decision


class RetryAfterPolicyTests(unittest.TestCase):
    def test_provider_hint_above_wait_budget_requires_failover(self) -> None:
        decision = retry_delay_decision(
            provider_hint="38",
            calculated_delay_seconds=2.0,
            wait_budget_seconds=20.0,
        )
        self.assertEqual(decision.action, "failover")
        self.assertIsNone(decision.delay_seconds)
        self.assertEqual(decision.provider_hint_seconds, 38.0)

    def test_provider_hint_inside_budget_is_never_shortened(self) -> None:
        decision = retry_delay_decision(
            provider_hint="18",
            calculated_delay_seconds=2.0,
            wait_budget_seconds=20.0,
        )
        self.assertEqual(decision.action, "retry")
        self.assertEqual(decision.delay_seconds, 18.0)

    def test_provider_hint_owns_delay_even_when_local_backoff_is_longer(self) -> None:
        decision = retry_delay_decision(
            provider_hint="1.4",
            calculated_delay_seconds=1.943,
            wait_budget_seconds=60.0,
        )
        self.assertEqual(decision.action, "retry")
        self.assertEqual(decision.delay_seconds, 1.4)
        self.assertEqual(decision.reason, "provider_hint_honored")

    def test_local_backoff_may_be_capped_when_no_provider_hint_exists(self) -> None:
        decision = retry_delay_decision(
            calculated_delay_seconds=30.0,
            wait_budget_seconds=20.0,
        )
        self.assertEqual(decision.action, "retry")
        self.assertEqual(decision.delay_seconds, 20.0)

    def test_invalid_or_negative_hint_is_not_provider_evidence(self) -> None:
        self.assertIsNone(parse_retry_after_seconds("later"))
        self.assertIsNone(parse_retry_after_seconds("-1"))
        decision = retry_delay_decision(
            provider_hint="later",
            calculated_delay_seconds=3.0,
            wait_budget_seconds=20.0,
        )
        self.assertEqual(decision.action, "retry")
        self.assertEqual(decision.delay_seconds, 3.0)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest

from scripts.orchestration_deadline import (
    AdmissionReason,
    DeadlineContractError,
    RunDeadlineBudget,
    StageDeadlinePolicy,
)


class FakeClock:
    def __init__(self, now_ms: int = 1_000_000) -> None:
        self.now_ms = now_ms

    def __call__(self) -> int:
        return self.now_ms

    def advance(self, ms: int) -> None:
        self.now_ms += ms


class DeadlinePolicyTests(unittest.TestCase):
    def test_rejects_invalid_policy(self) -> None:
        for kwargs in (
            dict(minimum_viable_ms=0, local_cap_ms=10, downstream_reserve_ms=0),
            dict(minimum_viable_ms=10, local_cap_ms=9, downstream_reserve_ms=0),
            dict(minimum_viable_ms=10, local_cap_ms=10, downstream_reserve_ms=-1),
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(DeadlineContractError):
                    StageDeadlinePolicy(**kwargs)


class RunDeadlineBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.budget = RunDeadlineBudget(10_000, clock_ms=self.clock)

    def policy(self, *, minimum: int = 2_000, cap: int = 5_000, reserve: int = 2_000):
        return StageDeadlinePolicy(
            minimum_viable_ms=minimum,
            local_cap_ms=cap,
            downstream_reserve_ms=reserve,
        )

    def test_absolute_run_deadline_is_created_once(self) -> None:
        start = self.budget.run_started_at_ms
        deadline = self.budget.run_deadline_at_ms
        self.clock.advance(3_000)
        self.assertEqual(self.budget.run_started_at_ms, start)
        self.assertEqual(self.budget.run_deadline_at_ms, deadline)
        self.assertEqual(self.budget.remaining_ms(), 7_000)

    def test_admission_rejects_when_minimum_plus_reserve_does_not_fit(self) -> None:
        self.clock.advance(6_500)
        decision = self.budget.admit_stage(self.policy())
        self.assertFalse(decision.admitted)
        self.assertEqual(decision.reason, AdmissionReason.EXHAUSTED_DEADLINE)
        self.assertEqual(decision.stage_budget_ms, 0)
        self.assertIsNone(decision.lease)

    def test_admission_budget_is_local_cap_when_room_exists(self) -> None:
        decision = self.budget.admit_stage(self.policy())
        self.assertTrue(decision.admitted)
        self.assertEqual(decision.stage_budget_ms, 5_000)
        self.assertIsNotNone(decision.lease)
        assert decision.lease is not None
        self.assertEqual(decision.lease.stage_deadline_at_ms, self.clock.now_ms + 5_000)

    def test_reserve_protection_caps_stage_before_run_deadline(self) -> None:
        policy = self.policy(minimum=1_000, cap=20_000, reserve=3_000)
        decision = self.budget.admit_stage(policy)
        self.assertTrue(decision.admitted)
        assert decision.lease is not None
        self.assertEqual(decision.stage_budget_ms, 7_000)
        self.assertEqual(
            decision.lease.stage_deadline_at_ms,
            self.budget.run_deadline_at_ms - 3_000,
        )

    def test_retry_uses_same_nonrenewable_lease(self) -> None:
        decision = self.budget.admit_stage(
            self.policy(minimum=1_000, cap=6_000, reserve=1_000)
        )
        assert decision.lease is not None
        lease = decision.lease
        original_deadline = lease.stage_deadline_at_ms
        self.clock.advance(2_500)
        self.assertEqual(lease.stage_deadline_at_ms, original_deadline)
        self.assertEqual(lease.remaining_ms(), 3_500)
        self.assertTrue(lease.can_honor_retry_after(3_500))
        self.assertFalse(lease.can_honor_retry_after(3_501))

    def test_retry_after_is_not_clipped_to_fit(self) -> None:
        decision = self.budget.admit_stage(
            self.policy(minimum=1_000, cap=4_000, reserve=1_000)
        )
        assert decision.lease is not None
        self.clock.advance(1_500)
        self.assertFalse(decision.lease.can_honor_retry_after(3_000))
        self.assertEqual(decision.lease.remaining_ms(), 2_500)

    def test_child_deadline_is_derived_from_parent_and_never_extends_it(self) -> None:
        decision = self.budget.admit_stage(
            self.policy(minimum=1_000, cap=5_000, reserve=1_000)
        )
        assert decision.lease is not None
        parent = decision.lease
        child = parent.derive_child_deadline(20_000)
        self.assertEqual(child.parent_deadline_at_ms, parent.stage_deadline_at_ms)
        self.assertEqual(child.deadline_at_ms, parent.stage_deadline_at_ms)

        self.clock.advance(1_000)
        short_child = parent.derive_child_deadline(1_500)
        self.assertEqual(short_child.deadline_at_ms, self.clock.now_ms + 1_500)
        self.assertLessEqual(short_child.deadline_at_ms, parent.stage_deadline_at_ms)

    def test_child_cannot_be_created_from_exhausted_parent(self) -> None:
        decision = self.budget.admit_stage(
            self.policy(minimum=1_000, cap=2_000, reserve=1_000)
        )
        assert decision.lease is not None
        self.clock.advance(2_000)
        with self.assertRaises(DeadlineContractError):
            decision.lease.derive_child_deadline(500)

    def test_provider_or_subprocess_timeout_never_exceeds_live_stage_budget(self) -> None:
        decision = self.budget.admit_stage(
            self.policy(minimum=1_000, cap=4_000, reserve=1_000)
        )
        assert decision.lease is not None
        self.assertEqual(decision.lease.timeout_ms(10_000), 4_000)
        self.clock.advance(1_250)
        self.assertEqual(decision.lease.timeout_ms(10_000), 2_750)

    def test_timeout_fails_closed_after_exhaustion(self) -> None:
        decision = self.budget.admit_stage(
            self.policy(minimum=1_000, cap=1_000, reserve=1_000)
        )
        assert decision.lease is not None
        self.clock.advance(1_000)
        with self.assertRaises(DeadlineContractError):
            decision.lease.timeout_ms(1)


if __name__ == "__main__":
    unittest.main()

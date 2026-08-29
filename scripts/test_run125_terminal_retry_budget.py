from __future__ import annotations

import unittest

from scripts import run124_terminal_provider_recovery as recovery


class Run125TerminalRetryBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.count = recovery._TERMINAL_RECOVERY_COUNT
        self.spent = recovery._TERMINAL_WAIT_SPENT_SECONDS

    def tearDown(self) -> None:
        recovery._TERMINAL_RECOVERY_COUNT = self.count
        recovery._TERMINAL_WAIT_SPENT_SECONDS = self.spent

    def test_run133_fourth_recovery_is_allowed_when_time_budget_has_room(self) -> None:
        recovery._TERMINAL_RECOVERY_COUNT = 3
        recovery._TERMINAL_WAIT_SPENT_SECONDS = 140.76
        self.assertTrue(recovery._run_wait_budget_allows(33.86))
        self.assertLessEqual(140.76 + 33.86, recovery._MAX_TERMINAL_WAIT_SECONDS_PER_RUN)

    def test_run134_fifth_recovery_is_allowed_when_topology_budget_has_room(self) -> None:
        # Exact Run134 evidence: S4-S7 consumed 155.59s and S8 needed 38.73s.
        # 194.32s is finite and valid for an eight-section Film graph, so an unrelated
        # fixed 180s ceiling must not reject it.
        recovery._TERMINAL_RECOVERY_COUNT = 4
        recovery._TERMINAL_WAIT_SPENT_SECONDS = 155.59
        self.assertTrue(recovery._run_wait_budget_allows(38.73))
        self.assertAlmostEqual(155.59 + 38.73, 194.32, places=2)
        self.assertLessEqual(194.32, recovery._MAX_TERMINAL_WAIT_SECONDS_PER_RUN)

    def test_runwide_wait_seconds_are_bounded(self) -> None:
        recovery._TERMINAL_RECOVERY_COUNT = 99
        recovery._TERMINAL_WAIT_SPENT_SECONDS = recovery._MAX_TERMINAL_WAIT_SECONDS_PER_RUN - 1.0
        self.assertFalse(recovery._run_wait_budget_allows(1.5))
        self.assertTrue(recovery._run_wait_budget_allows(1.0))

    def test_budget_contract_is_coherent_after_run134(self) -> None:
        # Provider reset evidence up to one minute remains eligible for terminal
        # recovery. Each actual sleep is capped at 60s, and the cumulative run budget
        # is derived from the largest configured long-form section graph rather than a
        # second independent magic number. Recovery count is telemetry only; retry-once
        # per terminal shard prevents loops.
        self.assertLessEqual(recovery._TERMINAL_RESET_LIMIT_SECONDS, 60.0)
        self.assertLessEqual(recovery._TERMINAL_WAIT_LIMIT_SECONDS, 60.0)
        self.assertLessEqual(
            recovery._TERMINAL_RESET_LIMIT_SECONDS,
            recovery._TERMINAL_WAIT_LIMIT_SECONDS,
        )
        self.assertGreaterEqual(recovery._RESET_SAFETY_SECONDS, 0.0)
        self.assertEqual(
            recovery._MAX_LONGFORM_SECTIONS,
            max(recovery.batching.staged._SECTION_COUNTS.values()),
        )
        self.assertEqual(
            recovery._MAX_TERMINAL_WAIT_SECONDS_PER_RUN,
            recovery._TERMINAL_WAIT_LIMIT_SECONDS * recovery._MAX_LONGFORM_SECTIONS,
        )


if __name__ == "__main__":
    unittest.main()

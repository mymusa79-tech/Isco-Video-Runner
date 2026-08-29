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

    def test_runwide_recovery_count_is_bounded(self) -> None:
        recovery._TERMINAL_RECOVERY_COUNT = recovery._MAX_TERMINAL_RECOVERIES_PER_RUN
        recovery._TERMINAL_WAIT_SPENT_SECONDS = 0.0
        self.assertFalse(recovery._run_wait_budget_allows(1.5))

    def test_runwide_wait_seconds_are_bounded(self) -> None:
        recovery._TERMINAL_RECOVERY_COUNT = 1
        recovery._TERMINAL_WAIT_SPENT_SECONDS = recovery._MAX_TERMINAL_WAIT_SECONDS_PER_RUN - 1.0
        self.assertFalse(recovery._run_wait_budget_allows(1.5))
        self.assertTrue(recovery._run_wait_budget_allows(1.0))

    def test_budget_contract_is_coherent_after_run132(self) -> None:
        # Provider reset evidence up to one minute remains eligible for the terminal
        # recovery path. The runtime caps the *actual sleep* at the separate wait limit,
        # so the safety tail never turns a 59-60s reset into a >60s sleep.
        self.assertLessEqual(recovery._TERMINAL_RESET_LIMIT_SECONDS, 60.0)
        self.assertLessEqual(recovery._TERMINAL_WAIT_LIMIT_SECONDS, 60.0)
        self.assertLessEqual(
            recovery._TERMINAL_RESET_LIMIT_SECONDS,
            recovery._TERMINAL_WAIT_LIMIT_SECONDS,
        )
        self.assertGreaterEqual(recovery._RESET_SAFETY_SECONDS, 0.0)
        self.assertLessEqual(recovery._MAX_TERMINAL_RECOVERIES_PER_RUN, 3)
        self.assertEqual(
            recovery._MAX_TERMINAL_WAIT_SECONDS_PER_RUN,
            recovery._TERMINAL_WAIT_LIMIT_SECONDS
            * recovery._MAX_TERMINAL_RECOVERIES_PER_RUN,
        )


if __name__ == "__main__":
    unittest.main()

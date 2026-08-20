from __future__ import annotations

import os
import unittest
from unittest import mock

from scripts import run_v3_voice


class P02ProductionBudgetEnforcementTests(unittest.TestCase):
    def test_v4_production_ledger_enables_enforcement_before_construction(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ISCO_AI_BUDGET_ENFORCE", None)
            ledger = run_v3_voice._production_budget_ledger("film")

            self.assertEqual(os.environ["ISCO_AI_BUDGET_ENFORCE"], "1")
            self.assertTrue(ledger._enforce)

    def test_production_ledger_preserves_requested_format(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            ledger = run_v3_voice._production_budget_ledger("story")
            self.assertEqual(ledger.to_summary()["format"], "story")


if __name__ == "__main__":
    unittest.main()

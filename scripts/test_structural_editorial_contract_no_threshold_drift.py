from __future__ import annotations

import inspect
import unittest

from scripts import structural_editorial_contract


class StructuralEditorialThresholdOwnershipTests(unittest.TestCase):
    def test_runner_does_not_duplicate_detector_thresholds_or_patterns(self) -> None:
        source = inspect.getsource(structural_editorial_contract)
        for forbidden in (
            "re.compile(",
            "re.findall(",
            "not_x_but_y =",
            "rhetorical_questions =",
            "generic_closers =",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()

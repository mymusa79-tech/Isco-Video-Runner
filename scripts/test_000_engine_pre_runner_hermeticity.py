from __future__ import annotations

import unittest

from scripts.engine_source_hermeticity import certify_engine_source_hermeticity


class EnginePreRunnerHermeticityTests(unittest.TestCase):
    def test_engine_suite_left_no_tracked_source_mutation(self) -> None:
        status = certify_engine_source_hermeticity("after_engine_before_runner")
        self.assertTrue(status.clean)


if __name__ == "__main__":
    unittest.main()

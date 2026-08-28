from __future__ import annotations

import unittest

from scripts.engine_source_hermeticity import certify_engine_source_hermeticity


class EnginePostRunnerHermeticityTests(unittest.TestCase):
    def test_runner_suite_left_no_tracked_engine_source_mutation(self) -> None:
        status = certify_engine_source_hermeticity("after_runner")
        self.assertTrue(status.clean)


if __name__ == "__main__":
    unittest.main()

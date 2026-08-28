from __future__ import annotations

import unittest

from scripts.engine_source_hermeticity import certify_engine_source_hermeticity


# unittest imports every requested module before it executes the suite. Certify at
# module import time so later Runner test-module imports cannot be misattributed to the
# Engine/CLI/Short pre-runner phase.
_PRE_RUNNER_STATUS = certify_engine_source_hermeticity(
    "before_full_runner_after_engine_cli_short"
)


class EnginePreRunnerHermeticityTests(unittest.TestCase):
    def test_pre_runner_engine_tree_was_clean_at_module_load(self) -> None:
        self.assertTrue(_PRE_RUNNER_STATUS.clean)


if __name__ == "__main__":
    unittest.main()

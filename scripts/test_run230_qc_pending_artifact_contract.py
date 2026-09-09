from __future__ import annotations

import unittest

from scripts.telegram_qc_pending_bridge_v1 import is_supported_qc_pending_artifact


class Run230QCPendingArtifactContractTests(unittest.TestCase):
    def test_only_exact_source_run_diagnostics_names_are_accepted(self) -> None:
        self.assertTrue(is_supported_qc_pending_artifact("isco-resilient-v4-diagnostics-230"))
        self.assertFalse(is_supported_qc_pending_artifact("isco-resilient-v4-diagnostics-"))
        self.assertFalse(is_supported_qc_pending_artifact("isco-resilient-v4-diagnostics-230-extra"))
        self.assertFalse(is_supported_qc_pending_artifact("random-artifact-230"))


if __name__ == "__main__":
    unittest.main()

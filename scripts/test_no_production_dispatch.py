from __future__ import annotations

import unittest
from pathlib import Path


class NoProductionDispatchTests(unittest.TestCase):
    def test_certification_sources_cannot_dispatch_canonical_production(self) -> None:
        # Certification code is allowed to *inspect* the workflow_dispatch token as
        # configuration.  What is forbidden here is an execution path that can
        # actually dispatch the canonical Production workflow.
        paths = [
            Path("scripts/provider_preflight.py"),
            Path("scripts/environment_preflight.py"),
            Path("scripts/preproduction_contract.py"),
            Path("scripts/release_transaction.py"),
            Path("scripts/state_persistence_strict.py"),
        ]
        forbidden_execution_markers = (
            "gh workflow run",
            "actions/workflows/produce-resilient-v4.yml/dispatches",
            "/actions/workflows/produce-resilient-v4.yml/dispatches",
        )
        for path in paths:
            text = path.read_text(encoding="utf-8")
            for marker in forbidden_execution_markers:
                with self.subTest(path=str(path), marker=marker):
                    self.assertNotIn(marker, text)


if __name__ == "__main__":
    unittest.main()

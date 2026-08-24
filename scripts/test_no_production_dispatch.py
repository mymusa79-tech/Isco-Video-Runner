from __future__ import annotations

import unittest
from pathlib import Path


class NoProductionDispatchTests(unittest.TestCase):
    def test_certification_sources_do_not_dispatch_canonical_production(self) -> None:
        paths = [
            Path("scripts/provider_preflight.py"),
            Path("scripts/environment_preflight.py"),
            Path("scripts/preproduction_contract.py"),
            Path("scripts/patch_production_workflow_for_preflight.py"),
        ]
        forbidden = ("workflow_dispatch", "gh workflow run", "actions/workflows/produce-resilient-v4.yml/dispatches")
        for path in paths:
            text = path.read_text(encoding="utf-8")
            for marker in forbidden:
                with self.subTest(path=str(path), marker=marker):
                    self.assertNotIn(marker, text)


if __name__ == "__main__":
    unittest.main()

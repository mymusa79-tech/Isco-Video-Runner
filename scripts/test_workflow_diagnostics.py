from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WorkflowDiagnosticsTests(unittest.TestCase):
    def test_v4_failure_upload_includes_factuality_audit(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "produce-resilient-v4.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("engine/output/*/factuality-audit.json", workflow)


if __name__ == "__main__":
    unittest.main()

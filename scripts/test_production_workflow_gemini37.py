from __future__ import annotations

import unittest
from pathlib import Path


WORKFLOW = Path(".github/workflows/produce-resilient-v4.yml")
CANONICAL = "GEMINI_CONTENT_MODEL: gemini-3.7-flash"
LEGACY = "GEMINI_CONTENT_MODEL: gemini-2.5-flash"


class ProductionWorkflowGemini37Tests(unittest.TestCase):
    def test_v4_uses_explicit_gemini37_for_preflight_and_production(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertEqual(text.count(CANONICAL), 2)
        self.assertNotIn(LEGACY, text)


if __name__ == "__main__":
    unittest.main()

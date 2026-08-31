from __future__ import annotations

import unittest
from pathlib import Path


WORKFLOW = Path(".github/workflows/produce-resilient-v4.yml")
TELEGRAM_GATEWAY = Path(".github/workflows/telegram-production-request.yml")
CANONICAL = "GEMINI_CONTENT_MODEL: gemini-3.7-flash"
LEGACY = "GEMINI_CONTENT_MODEL: gemini-2.5-flash"


class ProductionWorkflowGemini37Tests(unittest.TestCase):
    def test_v4_uses_explicit_gemini37_for_preflight_and_production(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertEqual(text.count(CANONICAL), 2)
        self.assertNotIn(LEGACY, text)

    def test_telegram_gateway_does_not_own_content_model_runtime(self) -> None:
        text = TELEGRAM_GATEWAY.read_text(encoding="utf-8")
        self.assertNotIn("GEMINI_CONTENT_MODEL", text)
        self.assertNotIn(LEGACY, text)
        self.assertIn("gh workflow run produce-resilient-v4.yml", text)


if __name__ == "__main__":
    unittest.main()

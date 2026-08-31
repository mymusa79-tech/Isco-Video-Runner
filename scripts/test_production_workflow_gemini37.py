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
        self.assertIn("actions/workflows/produce-resilient-v4.yml/dispatches", text)
        self.assertIn("return_run_details:true", text)
        self.assertIn("workflow_run_id", text)
        self.assertNotIn("repository: mymusa79-tech/Isco-Video-Agent", text)
        self.assertNotIn("GEMINI_API_KEY", text)
        self.assertNotIn("GROQ_API_KEY", text)
        self.assertNotIn("OPENROUTER_API_KEY", text)


if __name__ == "__main__":
    unittest.main()

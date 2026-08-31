from __future__ import annotations

import unittest
from pathlib import Path


WORKFLOW = Path(".github/workflows/produce-resilient-v4.yml")
CANONICAL = "GEMINI_CONTENT_MODEL: gemini-3.7-flash"
LEGACY = "GEMINI_CONTENT_MODEL: gemini-2.5-flash"

# 2026-08-31: this file only ever checked produce-resilient-v4.yml (the direct/manual
# dispatch path). telegram-production-request.yml - the workflow every real
# Telegram-triggered Short/Long production actually runs through - still shipped the
# legacy gemini-2.5-flash value two days after the canonical model moved to
# gemini-3.7-flash, and production_model_contract.py's drift guard failed closed on the
# very first real Telegram production attempt after that bump. Cover every production
# workflow that installs the model contract, not just the one exercised manually.
TELEGRAM_PRODUCTION_WORKFLOW = Path(".github/workflows/telegram-production-request.yml")


class ProductionWorkflowGemini37Tests(unittest.TestCase):
    def test_v4_uses_explicit_gemini37_for_preflight_and_production(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertEqual(text.count(CANONICAL), 2)
        self.assertNotIn(LEGACY, text)

    def test_telegram_production_request_uses_explicit_gemini37(self) -> None:
        text = TELEGRAM_PRODUCTION_WORKFLOW.read_text(encoding="utf-8")
        self.assertEqual(text.count(CANONICAL), 1)
        self.assertNotIn(LEGACY, text)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
ENGINE_SHA = "3d584c868b5c094d7856370f6d728aec841fef27"


class ProfessionalLongFormatEnginePinTests(unittest.TestCase):
    def test_v4_uses_new_engine_for_all_three_live_bindings(self) -> None:
        text = (ROOT / ".github" / "workflows" / "produce-resilient-v4.yml").read_text(encoding="utf-8")
        expected = re.findall(r"^\s*EXPECTED_ENGINE_SHA:\s*([0-9a-f]{40})\s*$", text, flags=re.MULTILINE)
        checkout = re.findall(
            r"repository:\s*mymusa79-tech/Isco-Video-Agent\s*\n\s*ref:\s*([0-9a-f]{40})\s*$",
            text,
            flags=re.MULTILINE,
        )
        runtime = re.findall(r"^\s*ISCO_ENGINE_SHA:\s*([0-9a-f]{40})\s*$", text, flags=re.MULTILINE)
        self.assertEqual(expected, [ENGINE_SHA])
        self.assertEqual(checkout, [ENGINE_SHA])
        self.assertEqual(runtime, [ENGINE_SHA])

    def test_telegram_dispatches_the_same_production_engine(self) -> None:
        text = (ROOT / ".github" / "workflows" / "telegram-editorial-control.yml").read_text(encoding="utf-8")
        production = re.findall(r"^\s*ENGINE_SHA:\s*([0-9a-f]{40})\s*$", text, flags=re.MULTILINE)
        self.assertEqual(production, [ENGINE_SHA])
        self.assertIn('-f engine_sha="$ENGINE_SHA"', text)


if __name__ == "__main__":
    unittest.main()

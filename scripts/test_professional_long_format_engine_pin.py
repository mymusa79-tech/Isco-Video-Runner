from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


def _canonical_v4_engine_sha() -> str:
    """Read the single production Engine authority from canonical V4.

    This deliberately avoids a second hard-coded Engine SHA in tests. When production
    advances the Engine, workflow_hygiene owns cross-workflow parity and this focused
    regression still proves V4's checkout/runtime/Telegram dispatch all resolve to the
    same full SHA. That prevents stale historical test constants from becoming a false
    release blocker while preserving the actual pin invariant.
    """
    text = (ROOT / ".github" / "workflows" / "produce-resilient-v4.yml").read_text(encoding="utf-8")
    expected = re.findall(
        r"^\s*EXPECTED_ENGINE_SHA:\s*([0-9a-f]{40})\s*$",
        text,
        flags=re.MULTILINE,
    )
    if len(expected) != 1 or not _FULL_SHA.fullmatch(expected[0]):
        raise AssertionError(
            "produce-resilient-v4.yml must expose exactly one canonical full EXPECTED_ENGINE_SHA"
        )
    return expected[0]


class ProfessionalLongFormatEnginePinTests(unittest.TestCase):
    def test_v4_uses_one_canonical_engine_for_all_three_live_bindings(self) -> None:
        text = (ROOT / ".github" / "workflows" / "produce-resilient-v4.yml").read_text(encoding="utf-8")
        canonical = _canonical_v4_engine_sha()
        expected = re.findall(
            r"^\s*EXPECTED_ENGINE_SHA:\s*([0-9a-f]{40})\s*$",
            text,
            flags=re.MULTILINE,
        )
        checkout = re.findall(
            r"repository:\s*mymusa79-tech/Isco-Video-Agent\s*\n\s*ref:\s*([0-9a-f]{40})\s*$",
            text,
            flags=re.MULTILINE,
        )
        runtime = re.findall(
            r"^\s*ISCO_ENGINE_SHA:\s*([0-9a-f]{40})\s*$",
            text,
            flags=re.MULTILINE,
        )
        self.assertEqual(expected, [canonical])
        self.assertEqual(checkout, [canonical])
        self.assertEqual(runtime, [canonical])

    def test_telegram_dispatches_the_same_canonical_production_engine(self) -> None:
        canonical = _canonical_v4_engine_sha()
        text = (ROOT / ".github" / "workflows" / "telegram-editorial-control.yml").read_text(encoding="utf-8")
        production = re.findall(
            r"^\s*ENGINE_SHA:\s*([0-9a-f]{40})\s*$",
            text,
            flags=re.MULTILINE,
        )
        self.assertEqual(production, [canonical])
        self.assertIn('-f engine_sha="$ENGINE_SHA"', text)


if __name__ == "__main__":
    unittest.main()

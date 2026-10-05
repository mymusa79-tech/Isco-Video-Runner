from __future__ import annotations

import unittest
from pathlib import Path


class LegacyTelegramProductionFreshnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = Path(".github/workflows/clean-v2-telegram-production.yml").read_text(
            encoding="utf-8"
        )

    def test_stale_queued_runner_is_rejected_before_provider_work(self) -> None:
        verify = self.text.index("Verify exact Runner checkout")
        freshness = self.text.index("Check queued Runner freshness")
        engine = self.text.index("Checkout frozen private Engine source")
        self.assertLess(verify, freshness)
        self.assertLess(freshness, engine)
        block = self.text[freshness:engine]
        self.assertIn("git fetch --no-tags origin main:refs/remotes/origin/main", block)
        self.assertIn('if [ "$GITHUB_SHA" = "$current_main" ]', block)
        self.assertIn('echo "stale=true"', block)
        self.assertIn("Reject stale queued Runner before production", block)
        self.assertIn("exit 75", block)
        self.assertIn("أوقفت محاولة إنتاج قديمة", block)

    def test_stale_rejection_does_not_emit_fake_terminal_artifacts(self) -> None:
        for marker in (
            "Prepare exact Telegram request checkpoint",
            "Prepare verified Gemini TTS chunks",
            "Persist recent narrative and Podcast promo history",
            "Upload Telegram Clean V2 production evidence",
            "Telegram terminal watchdog",
            "Record Telegram production terminal history",
        ):
            start = self.text.index(f"- name: {marker}")
            block = self.text[start : start + 260]
            self.assertIn("steps.runner_freshness.outputs.stale != 'true'", block)

    def test_both_production_paths_skip_gemini_only_for_visual_qa(self) -> None:
        self.assertIn('CLEAN_V2_VISUAL_SKIP_GEMINI: "true"', self.text)
        v4 = Path(".github/workflows/produce-resilient-v4.yml").read_text(encoding="utf-8")
        self.assertIn('CLEAN_V2_VISUAL_SKIP_GEMINI: "true"', v4)
        self.assertIn("GEMINI_TTS_MODEL", self.text)
        self.assertIn("GEMINI_TTS_MODEL", v4)


if __name__ == "__main__":
    unittest.main()

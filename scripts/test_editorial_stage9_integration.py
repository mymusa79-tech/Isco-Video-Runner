from __future__ import annotations

import unittest
from pathlib import Path


ENGINE_SHA = "568da9edfb68ebf9ea6e7d6aed0b6a9ee9a1180a"
OLD_ENGINE_SHA = "860b61b0f99b285cc2917cba079e29bfa9e2c119"


class EditorialStage9IntegrationTests(unittest.TestCase):
    def test_all_live_engine_pins_match_stage8_main(self) -> None:
        production = Path(".github/workflows/produce-resilient-v4.yml").read_text(encoding="utf-8")
        telegram = Path(".github/workflows/telegram-editorial-control.yml").read_text(encoding="utf-8")
        hei = Path(".github/workflows/verify-human-editorial-intent-m7.yml").read_text(encoding="utf-8")
        self.assertEqual(production.count(ENGINE_SHA), 3)
        self.assertEqual(telegram.count(ENGINE_SHA), 1)
        self.assertEqual(hei.count(ENGINE_SHA), 1)
        for text in (production, telegram, hei):
            self.assertNotIn(OLD_ENGINE_SHA, text)

    def test_production_keeps_manual_dispatch_and_telegram_production_lock(self) -> None:
        production = Path(".github/workflows/produce-resilient-v4.yml").read_text(encoding="utf-8")
        telegram = Path(".github/workflows/telegram-editorial-control.yml").read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", production)
        self.assertNotIn("\n  push:", production)
        self.assertNotIn("\n  schedule:", production)
        self.assertIn('CONTROL_PLANE_PRODUCTION_ENABLED: "false"', telegram)

    def test_one_production_id_is_bound_before_engine_and_reused_after_gold(self) -> None:
        runner = Path("scripts/run_v3_voice.py").read_text(encoding="utf-8")
        self.assertEqual(runner.count("production_id = _production_id()"), 1)
        self.assertEqual(runner.count('os.environ["ISCO_PRODUCTION_ID"] = production_id'), 1)
        before = runner.index("orchestrator.produce(")
        binding = runner.index('os.environ["ISCO_PRODUCTION_ID"] = production_id')
        manifest = runner.index("_write_production_manifest(out, production_id=production_id")
        analytics = runner.index("production_id=production_id if manifest.get")
        self.assertLess(binding, before)
        self.assertLess(before, manifest)
        self.assertLess(manifest, analytics)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
from pathlib import Path


# Stage 9 final integration contract: exact current Engine pin + one production provenance id.
ENGINE_SHA = "0370105f0b7212b02e3c53d8e64eefd83e7fd8fe"
OLD_ENGINE_SHA = "39d4a0ea613cf266c7b4c561acb4a01216909cd9"


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

    def test_production_keeps_manual_canonical_dispatch_and_explicit_telegram_start(self) -> None:
        production = Path(".github/workflows/produce-resilient-v4.yml").read_text(encoding="utf-8")
        telegram = Path(".github/workflows/telegram-editorial-control.yml").read_text(encoding="utf-8")
        telegram_target = Path(".github/workflows/telegram-production-request.yml").read_text(encoding="utf-8")

        self.assertIn("workflow_dispatch:", production)
        self.assertNotIn("\n  push:", production)
        self.assertNotIn("\n  schedule:", production)

        self.assertIn('CONTROL_PLANE_PRODUCTION_ENABLED: "true"', telegram)
        self.assertIn("Reserve explicit production dispatch", telegram)
        self.assertIn("Persist dispatch reservation before workflow dispatch", telegram)
        self.assertIn("gh workflow run telegram-production-request.yml", telegram)
        self.assertIn('-f authorization_id="$AUTHORIZATION_ID"', telegram)
        self.assertNotIn("python scripts/run_control_production.py", telegram)

        target_header = telegram_target[: telegram_target.index("jobs:")]
        self.assertIn("workflow_dispatch:", target_header)
        self.assertNotIn("\n  push:", target_header)
        self.assertNotIn("\n  schedule:", target_header)
        self.assertNotIn("\n  pull_request:", target_header)
        self.assertIn("authorization_id:", target_header)
        self.assertIn("validate_dispatch_authorization", telegram_target)
        self.assertIn("telegram_production_queue.py consume", telegram_target)

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

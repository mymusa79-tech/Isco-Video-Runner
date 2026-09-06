from __future__ import annotations

import unittest
from pathlib import Path

from scripts.workflow_hygiene import _canonical_engine_pin


# Historical pins are rejection sentinels only. The current Engine identity is
# read from the canonical Production V4 workflow so this integration test cannot
# become a second source of truth.
OLD_ENGINE_SHA = "4f726df0d9dd20c68bc0c0f096320dc9c7369aeb"
STALE_ENGINE_SHA = "f3c9357098947882882ca3010b46a565c2d90460"
LIVE_ENGINE_WORKFLOWS = {
    "produce-resilient-v4.yml": 3,
    "telegram-editorial-control.yml": 1,
    "telegram-production-request.yml": 1,
    "verify-human-editorial-intent-m7.yml": 1,
    "verify-m11-live-integration.yml": 1,
}


class EditorialStage9IntegrationTests(unittest.TestCase):
    def test_all_live_engine_pins_match_canonical_production(self) -> None:
        workflow_dir = Path(".github/workflows")
        engine_sha = _canonical_engine_pin(workflow_dir)
        self.assertIsNotNone(engine_sha)
        assert engine_sha is not None

        for name, expected_count in LIVE_ENGINE_WORKFLOWS.items():
            text = (workflow_dir / name).read_text(encoding="utf-8")
            self.assertEqual(text.count(engine_sha), expected_count, name)
            self.assertNotIn(OLD_ENGINE_SHA, text, name)
            self.assertNotIn(STALE_ENGINE_SHA, text, name)

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

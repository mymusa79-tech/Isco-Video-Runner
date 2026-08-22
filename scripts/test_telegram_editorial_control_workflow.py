from __future__ import annotations

import unittest
from pathlib import Path


ENGINE_SHA = "089a97adde5d2e64b35262f944865241384f1429"
OLD_ENGINE_SHA = "568da9edfb68ebf9ea6e7d6aed0b6a9ee9a1180a"


class TelegramEditorialControlWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = Path(".github/workflows/telegram-editorial-control.yml").read_text(encoding="utf-8")

    def test_production_is_hard_locked(self):
        self.assertIn('CONTROL_PLANE_PRODUCTION_ENABLED: "false"', self.text)
        self.assertNotIn("workflow run produce-resilient-v4", self.text)
        self.assertNotIn("gh workflow run", self.text)
        self.assertNotIn("run_control_production.py", self.text)

    def test_polling_is_paused_while_production_v4_is_active(self):
        self.assertIn("produce-resilient-v4.yml/runs?per_page=20", self.text)
        self.assertIn('select(.status != "completed")', self.text)
        self.assertIn("Telegram polling paused", self.text)

    def test_first_activation_ignores_historical_telegram_updates(self):
        self.assertIn("Prime Telegram offset on first activation", self.text)
        self.assertIn("historical updates ignored", self.text)

    def test_control_state_is_encrypted_at_rest_with_dedicated_state_key(self):
        self.assertIn("state/control-panel.json.enc", self.text)
        self.assertIn("openssl enc -aes-256-cbc -salt -pbkdf2", self.text)
        self.assertEqual(self.text.count("STATE_ENCRYPTION_KEY: ${{ secrets.STATE_ENCRYPTION_KEY }}"), 2)
        self.assertNotIn("secrets.STATE_ENCRYPTION_KEY || secrets.TELEGRAM_BOT_TOKEN", self.text)
        self.assertNotIn("state/control-panel.json\n", self.text)

    def test_engine_is_only_checked_out_when_research_is_pending(self):
        self.assertIn("if: steps.poll.outputs.needs_engine == 'true'", self.text)
        self.assertIn(ENGINE_SHA, self.text)
        self.assertNotIn(OLD_ENGINE_SHA, self.text)

    def test_schedule_runs_simple_client_surface_only(self):
        self.assertIn('cron: "*/5 * * * *"', self.text)
        self.assertIn("python scripts/telegram_control_simple_ui.py poll", self.text)
        self.assertIn("python scripts/telegram_control_simple_ui.py research", self.text)
        self.assertNotIn("python scripts/telegram_control_panel.py poll", self.text)


if __name__ == "__main__":
    unittest.main()

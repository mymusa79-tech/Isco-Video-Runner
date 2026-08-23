from __future__ import annotations

import unittest
from pathlib import Path


ENGINE_SHA = "64ab711bc904e9581c3cc6c8280d1321ae738eb1"
OLD_ENGINE_SHA = "089a97adde5d2e64b35262f944865241384f1429"


class TelegramEditorialControlWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = Path(".github/workflows/telegram-editorial-control.yml").read_text(encoding="utf-8")

    def test_production_capability_is_enabled_but_only_explicitly_dispatched(self):
        self.assertIn('CONTROL_PLANE_PRODUCTION_ENABLED: "true"', self.text)
        self.assertIn("actions: write", self.text)
        self.assertIn("if: steps.poll.outputs.needs_production == 'true'", self.text)
        self.assertIn("gh workflow run telegram-production-request.yml", self.text)
        self.assertIn('-f request_id="$REQUEST_ID"', self.text)
        self.assertIn('-f request_sha256="$REQUEST_SHA256"', self.text)
        self.assertIn('-f engine_sha="$ENGINE_SHA"', self.text)
        self.assertNotIn("python scripts/run_control_production.py", self.text)

    def test_dispatch_is_marked_only_after_workflow_dispatch_succeeds(self):
        dispatch = self.text.index("gh workflow run telegram-production-request.yml")
        mark = self.text.index("telegram_production_queue.py mark-dispatched")
        self.assertLess(dispatch, mark)
        self.assertIn('--state "$CONTROL_STATE_PATH"', self.text)

    def test_polling_is_paused_while_canonical_production_v4_is_active(self):
        self.assertIn("produce-resilient-v4.yml/runs?per_page=20", self.text)
        self.assertIn('select(.status != "completed")', self.text)
        self.assertIn("Telegram polling paused", self.text)
        self.assertIn('echo "needs_production=false"', self.text)

    def test_first_activation_ignores_historical_telegram_updates(self):
        self.assertIn("Prime Telegram offset on first activation", self.text)
        self.assertIn("historical updates ignored", self.text)

    def test_control_state_is_encrypted_at_rest_with_dedicated_state_key(self):
        self.assertIn("state/control-panel.json.enc", self.text)
        self.assertIn("openssl enc -aes-256-cbc -salt -pbkdf2", self.text)
        self.assertEqual(self.text.count("STATE_ENCRYPTION_KEY: ${{ secrets.STATE_ENCRYPTION_KEY }}"), 2)
        self.assertNotIn("secrets.STATE_ENCRYPTION_KEY || secrets.TELEGRAM_BOT_TOKEN", self.text)
        self.assertIn('"production_queue":[]', self.text)

    def test_engine_is_only_checked_out_for_research_in_control_workflow(self):
        self.assertIn("if: steps.poll.outputs.needs_engine == 'true'", self.text)
        self.assertIn(ENGINE_SHA, self.text)
        self.assertNotIn(OLD_ENGINE_SHA, self.text)

    def test_schedule_runs_active_explicit_start_surface(self):
        self.assertIn('cron: "*/5 * * * *"', self.text)
        self.assertIn("python scripts/telegram_control_active_ui.py poll", self.text)
        self.assertIn("python scripts/telegram_control_active_ui.py research", self.text)
        self.assertNotIn("python scripts/telegram_control_panel.py poll", self.text)


if __name__ == "__main__":
    unittest.main()

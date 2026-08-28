from __future__ import annotations

import unittest
from pathlib import Path


ENGINE_SHA = "f3c9357098947882882ca3010b46a565c2d90460"
OLD_ENGINE_SHA = "39d4a0ea613cf266c7b4c561acb4a01216909cd9"


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
        self.assertIn('-f authorization_id="$AUTHORIZATION_ID"', self.text)
        self.assertIn('-f engine_sha="$ENGINE_SHA"', self.text)
        self.assertNotIn("python scripts/run_control_production.py", self.text)

    def test_dispatch_is_durably_reserved_before_workflow_launch(self):
        reserve = self.text.index("telegram_production_queue.py reserve")
        durable = self.text.index("Persist dispatch reservation before workflow dispatch")
        dispatch = self.text.index("gh workflow run telegram-production-request.yml")
        self.assertLess(reserve, durable)
        self.assertLess(durable, dispatch)
        self.assertIn('control-panel.reserved.json.enc', self.text)
        self.assertIn('state: reserve explicit Telegram production dispatch', self.text)
        self.assertIn('--github-output "$GITHUB_OUTPUT"', self.text)
        self.assertIn('AUTHORIZATION_ID: ${{ steps.reserve.outputs.production_authorization_id }}', self.text)
        self.assertNotIn("telegram_production_queue.py mark-dispatched", self.text)

    def test_dedicated_production_workflow_owns_authorization_consumption(self):
        self.assertIn('-f authorization_id="$AUTHORIZATION_ID"', self.text)
        self.assertIn(
            "if: always() && steps.poll.outputs.needs_production != 'true' && steps.poll.outputs.production_active != 'true'",
            self.text,
        )
        reservation = self.text.index("Persist dispatch reservation before workflow dispatch")
        dispatch = self.text.index("gh workflow run telegram-production-request.yml")
        generic_persist = self.text.index("Persist encrypted control-panel state")
        self.assertLess(reservation, dispatch)
        self.assertLess(dispatch, generic_persist)

    def test_polling_is_read_only_while_any_production_path_is_active(self):
        self.assertIn("produce-resilient-v4.yml/runs?per_page=20", self.text)
        self.assertIn("telegram-production-request.yml/runs?per_page=20", self.text)
        self.assertIn('active=$((active_v4 + active_telegram))', self.text)
        self.assertIn('select(.status != "completed")', self.text)
        self.assertIn("stateful Telegram control remains read-only", self.text)
        self.assertIn('echo "production_active=true"', self.text)
        self.assertIn('echo "production_active=false"', self.text)
        self.assertIn('echo "needs_production=false"', self.text)

    def test_first_activation_ignores_historical_telegram_updates(self):
        self.assertIn("Prime Telegram offset on first activation", self.text)
        self.assertIn("historical updates ignored", self.text)

    def test_control_state_is_encrypted_at_rest_with_dedicated_state_key(self):
        self.assertIn("state/control-panel.json.enc", self.text)
        self.assertIn("openssl enc -aes-256-cbc -salt -pbkdf2", self.text)
        self.assertGreaterEqual(self.text.count("STATE_ENCRYPTION_KEY: ${{ secrets.STATE_ENCRYPTION_KEY }}"), 3)
        self.assertNotIn("secrets.STATE_ENCRYPTION_KEY || secrets.TELEGRAM_BOT_TOKEN", self.text)
        self.assertIn('"production_queue":[]', self.text)

    def test_engine_is_only_checked_out_for_research_in_control_workflow(self):
        self.assertIn("if: steps.poll.outputs.needs_engine == 'true'", self.text)
        self.assertIn(ENGINE_SHA, self.text)
        self.assertNotIn(OLD_ENGINE_SHA, self.text)

    def test_research_step_carries_an_openrouter_fallback_key(self):
        research_step = self.text.index("Execute one pending editorial research request")
        next_step = self.text.index("Reserve explicit production dispatch")
        segment = self.text[research_step:next_step]
        self.assertIn("OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}", segment)
        self.assertIn("GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}", segment)

    def test_schedule_runs_topic_memory_policy_entrypoint(self):
        self.assertIn('cron: "*/5 * * * *"', self.text)
        self.assertIn("python scripts/telegram_topic_memory_ui.py poll", self.text)
        self.assertIn("python scripts/telegram_topic_memory_ui.py research", self.text)
        self.assertNotIn("python scripts/telegram_control_active_ui.py poll", self.text)
        self.assertNotIn("python scripts/telegram_control_active_ui.py research", self.text)
        self.assertNotIn("python scripts/telegram_control_panel.py poll", self.text)


if __name__ == "__main__":
    unittest.main()

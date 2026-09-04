from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import telegram_topic_research_v2 as topic_entry
from scripts import telegram_webhook_replay as webhook_entry


class TopicResearchRuntimeRecoveryTests(unittest.TestCase):
    def test_webhook_gate_yields_scheduler_when_durable_research_is_pending(self):
        with tempfile.NamedTemporaryFile(mode="w+", encoding="utf-8") as handle:
            json.dump({"requests": {}, "production_queue": [], "used_topics": [], "saved_suggestions": []}, handle)
            handle.flush()
            previous = os.environ.get("CONTROL_STATE_PATH")
            os.environ["CONTROL_STATE_PATH"] = handle.name
            try:
                with patch.object(webhook_entry.core, "webhook_active", return_value=True), patch.object(
                    webhook_entry, "_durable_pending_research_exists", return_value=True
                ), patch.object(sys, "argv", ["telegram_webhook_replay.py", "webhook-active"]):
                    with self.assertRaises(SystemExit) as raised:
                        webhook_entry.main()
                self.assertEqual(raised.exception.code, 1)
            finally:
                if previous is None:
                    os.environ.pop("CONTROL_STATE_PATH", None)
                else:
                    os.environ["CONTROL_STATE_PATH"] = previous

    def test_webhook_gate_still_suppresses_polling_when_no_pending_research_exists(self):
        with patch.object(webhook_entry, "_reconcile_used_history_if_available", return_value=None), patch.object(
            webhook_entry.core, "webhook_active", return_value=True
        ), patch.object(webhook_entry, "_durable_pending_research_exists", return_value=False), patch.object(
            sys, "argv", ["telegram_webhook_replay.py", "webhook-active"]
        ):
            with self.assertRaises(SystemExit) as raised:
                webhook_entry.main()
        self.assertEqual(raised.exception.code, 0)

    def test_webhook_health_pass_reconciles_used_history_before_exit(self):
        calls: list[str] = []
        with patch.object(
            webhook_entry,
            "_reconcile_used_history_if_available",
            side_effect=lambda: calls.append("reconcile") or {"processed": 0, "added": 0},
        ), patch.object(
            webhook_entry.core,
            "webhook_active",
            side_effect=lambda: calls.append("webhook") or True,
        ), patch.object(
            webhook_entry, "_durable_pending_research_exists", return_value=False
        ), patch.object(sys, "argv", ["telegram_webhook_replay.py", "webhook-active"]):
            with self.assertRaises(SystemExit) as raised:
                webhook_entry.main()
        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(calls, ["reconcile", "webhook"])

    def test_scheduled_retry_claims_engine_without_getupdates_when_webhook_is_active(self):
        outputs: list[tuple[str, str]] = []
        with patch.object(topic_entry, "_durable_pending_research_exists", return_value=True), patch(
            "scripts.telegram_webhook_replay_core.webhook_active", return_value=True
        ), patch.object(topic_entry.core.panel, "_github_output", side_effect=lambda k, v: outputs.append((k, v))), patch.object(
            sys, "argv", ["telegram_topic_research_v2.py", "poll", "--state", "/tmp/control-panel.json"]
        ):
            claimed = topic_entry._claim_pending_scheduler_retry_without_polling("poll")
        self.assertTrue(claimed)
        self.assertEqual(outputs, [("needs_engine", "true"), ("needs_production", "false")])

    def test_research_entrypoint_installs_existing_live_provider_reliability_adapter(self):
        text = Path("scripts/telegram_topic_research_v2.py").read_text(encoding="utf-8")
        self.assertIn("gemini_research_call_with_fallback", text)
        self.assertIn("engine_research.json_text = gemini_research_call_with_fallback", text)
        self.assertIn('_install_live_topic_provider_reliability(mode)', text)
        self.assertNotIn("fallback_topics", text)


if __name__ == "__main__":
    unittest.main()

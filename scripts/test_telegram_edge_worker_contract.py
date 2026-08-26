from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "cloudflare" / "telegram-control-worker" / "index.js"


class TelegramEdgeWorkerContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = WORKER.read_text(encoding="utf-8")

    def test_worker_has_valid_javascript_syntax_when_node_is_available(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is unavailable")
        subprocess.run([node, "--check", str(WORKER)], check=True, capture_output=True, text=True)

    def test_webhook_requires_secret_header_and_exact_identity_boundaries(self):
        self.assertIn("X-Telegram-Bot-Api-Secret-Token", self.text)
        self.assertIn("TELEGRAM_WEBHOOK_SECRET", self.text)
        self.assertIn("TELEGRAM_ALLOWED_USER_ID", self.text)
        self.assertIn("TELEGRAM_CHAT_ID", self.text)
        self.assertIn("secretHeaderValid(request, env)", self.text)
        self.assertIn("authorized(update, env)", self.text)

    def test_edge_never_dispatches_production_workflow_directly(self):
        self.assertIn('const WORKFLOW = "telegram-editorial-control.yml"', self.text)
        self.assertNotIn("telegram-production-request.yml", self.text)
        self.assertNotIn("produce-resilient-v4.yml", self.text)
        self.assertIn("GITHUB_CONTROL_TOKEN", self.text)

    def test_exact_text_confirmation_is_forwarded_not_executed_at_edge(self):
        self.assertIn('const CONFIRM_TEXT = "تأكيد الإنتاج"', self.text)
        self.assertIn('if (value === CONFIRM_TEXT) return "stateful"', self.text)
        self.assertIn("dispatchToGitHub(env, update)", self.text)

    def test_operations_toggle_is_same_message_bound(self):
        self.assertIn("Number(target.messageId) !== boundMessageId", self.text)
        self.assertIn('telegram(env, "editMessageText"', self.text)
        self.assertIn("cmd:ops", self.text)

    def test_fast_navigation_and_stats_are_edge_local(self):
        for callback in (
            "cmd:menu",
            "cmd:search_menu",
            "cmd:library_menu",
            "cmd:stats_menu",
            "cmd:stats_last_long",
            "cmd:stats_last_short",
            "cmd:stats_today",
            "cmd:stats_week",
            "cmd:stats_overview",
        ):
            self.assertIn(callback, self.text)
        self.assertIn("sendStats(env", self.text)

    def test_retry_is_not_added_to_v1_surface(self):
        self.assertNotIn("cmd:retry", self.text)
        self.assertNotIn("retry:", self.text)


if __name__ == "__main__":
    unittest.main()

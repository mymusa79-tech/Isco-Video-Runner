from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASE_WORKER = ROOT / "cloudflare" / "telegram-control-worker" / "index.js"
V2_WORKER = ROOT / "cloudflare" / "telegram-control-worker" / "index-v2.js"
WRANGLER = ROOT / "cloudflare" / "telegram-control-worker" / "wrangler.toml.example"


class TelegramEdgeWorkerContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base_text = BASE_WORKER.read_text(encoding="utf-8")
        cls.v2_text = V2_WORKER.read_text(encoding="utf-8")
        cls.text = cls.base_text + "\n" + cls.v2_text
        cls.wrangler = WRANGLER.read_text(encoding="utf-8")

    def test_workers_have_valid_javascript_syntax_when_node_is_available(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is unavailable")
        for worker in (BASE_WORKER, V2_WORKER):
            subprocess.run([node, "--check", str(worker)], check=True, capture_output=True, text=True)

    def test_wrangler_activates_v2_wrapper(self):
        self.assertIn('main = "index-v2.js"', self.wrangler)
        self.assertIn('import base from "./index.js"', self.v2_text)
        self.assertIn("return base.fetch(request, env, ctx)", self.v2_text)

    def test_webhook_requires_secret_header_and_exact_identity_boundaries(self):
        self.assertIn("X-Telegram-Bot-Api-Secret-Token", self.v2_text)
        self.assertIn("TELEGRAM_WEBHOOK_SECRET", self.v2_text)
        self.assertIn("TELEGRAM_ALLOWED_USER_ID", self.v2_text)
        self.assertIn("TELEGRAM_CHAT_ID", self.v2_text)
        self.assertIn("secretHeaderValid(request, env)", self.v2_text)
        self.assertIn("authorized(update, env)", self.v2_text)

    def test_edge_never_dispatches_production_workflow_directly(self):
        self.assertIn('const WORKFLOW = "telegram-editorial-control.yml"', self.base_text)
        self.assertNotIn("telegram-production-request.yml", self.text)
        self.assertNotIn("produce-resilient-v4.yml", self.text)
        self.assertIn("GITHUB_CONTROL_TOKEN", self.base_text)

    def test_exact_text_confirmation_remains_owned_by_base_control_plane(self):
        self.assertIn('const CONFIRM_TEXT = "تأكيد الإنتاج"', self.base_text)
        self.assertIn('if (value === CONFIRM_TEXT) return "stateful"', self.base_text)
        self.assertIn("dispatchToGitHub(env, update)", self.base_text)
        self.assertNotIn("dispatchToGitHub", self.v2_text)

    def test_operations_toggle_remains_same_message_bound(self):
        self.assertIn("Number(target.messageId) !== boundMessageId", self.base_text)
        self.assertIn('telegram(env, "editMessageText"', self.base_text)
        self.assertIn("cmd:ops", self.base_text)

    def test_v2_navigation_reuses_same_message_surface(self):
        self.assertIn("await edit(env, target.chatId, target.messageId, menu[0], menu[1])", self.v2_text)
        self.assertIn('style: "primary"', self.v2_text)
        self.assertIn("ماذا تريد أن تفعل الآن؟", self.v2_text)

    def test_fast_navigation_and_visual_stats_are_edge_local(self):
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
            self.assertIn(callback, self.v2_text)
        self.assertIn("sendStatsCard(env", self.v2_text)
        self.assertIn('telegram(env, "sendPhoto"', self.v2_text)
        self.assertIn("channelThumbnail", self.v2_text)
        self.assertIn("thumbnail: bestThumbnail", self.v2_text)

    def test_research_ack_is_explicit_and_never_claims_production_started(self):
        self.assertIn("بدأ بحث الحلقة", self.v2_text)
        self.assertIn("3 خيارات مرقمة", self.v2_text)
        self.assertIn("لا يبدأ أي Production من البحث", self.v2_text)
        self.assertIn("حصة Gemini المجانية", self.v2_text)

    def test_retry_button_is_not_a_production_retry_surface(self):
        self.assertNotIn("cmd:retry", self.text)
        self.assertNotIn("retry:", self.text)


if __name__ == "__main__":
    unittest.main()

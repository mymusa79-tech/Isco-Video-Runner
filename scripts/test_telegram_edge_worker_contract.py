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
        self.assertIn("dispatchToGitHub(env, update)", self.text)

    def test_exact_text_confirmation_is_forwarded_not_executed_at_edge(self):
        self.assertIn('const CONFIRM_TEXT = "تأكيد الإنتاج"', self.text)
        self.assertIn('if (value === CONFIRM_TEXT) return "stateful"', self.text)
        self.assertIn("dispatchToGitHub(env, update)", self.text)

    def test_operations_toggle_is_same_message_bound(self):
        self.assertIn("Number(target.messageId) !== boundMessageId", self.text)
        self.assertIn('telegram(env, "editMessageText"', self.text)
        self.assertIn("cmd:ops", self.text)

    def test_fast_navigation_stats_status_and_delivery_are_edge_local(self):
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
            "cmd:status",
            "cmd:last_delivery",
        ):
            self.assertIn(callback, self.text)
        self.assertIn("sendStats(env", self.text)
        self.assertIn("function isReadOnlyLeaf(data)", self.text)
        self.assertIn("sendProductionStatus(env, target)", self.text)
        self.assertIn("sendLastDelivery(env, target)", self.text)
        self.assertIn("isReadOnlyLeaf(target.data)", self.text)

    def test_live_status_uses_real_github_run_steps_without_fake_substage(self):
        self.assertIn("actions/runs?per_page=50", self.text)
        self.assertIn("runJobs(env, run)", self.text)
        self.assertIn("currentRunStep(jobs)", self.text)
        self.assertIn("الخطوة الحالية في GitHub Actions", self.text)
        self.assertIn("الإنتاج: التخطيط → الكتابة → الصوت → المونتاج", self.text)
        self.assertIn('disabled: {}', self.text)
        self.assertIn('callback_data: "cmd:status"', self.text)

    def test_bot_api_10_3_ephemeral_rich_reply_has_exact_receiver_boundary(self):
        self.assertIn('telegram(env, "sendRichMessage"', self.text)
        self.assertIn("ephemeral_message_parameters", self.text)
        self.assertIn("receiver_user_id: Number(target.userId)", self.text)
        self.assertIn("callback_query_id: target.callbackId", self.text)
        self.assertIn("replace_callback_query_message: true", self.text)
        self.assertIn("Bot API 10.3 rich surfaces are progressive enhancement", self.text)

    def test_quality_gates_are_read_from_deterministic_release_assets(self):
        self.assertIn('"quality-final.json"', self.text)
        self.assertIn('"final-master-qc.json"', self.text)
        self.assertIn("latestQualityGates(env", self.text)
        self.assertIn("🧪 Quality Gates", self.text)
        self.assertIn("هذه شاشة قراءة فقط", self.text)

    def test_last_delivery_embeds_safe_release_assets_and_keeps_release_link(self):
        self.assertIn("MAX_RICH_DELIVERY_FILES", self.text)
        self.assertIn("MAX_TELEGRAM_DOCUMENT_BYTES", self.text)
        self.assertIn("browser_download_url", self.text)
        self.assertIn('type: "document", document: { type: "document", media: asset.browser_download_url }', self.text)
        self.assertIn("فتح Release", self.text)
        self.assertIn('callback_data: "cmd:last_delivery"', self.text)

    def test_stateful_edge_ack_explains_the_next_visible_result(self):
        self.assertIn("function statefulCallbackAck(data)", self.text)
        self.assertIn('data === "cmd:topic"', self.text)
        self.assertIn("بدأ بحث الحلقة", self.text)
        self.assertIn("3 أفكار مرقمة للاختيار", self.text)
        self.assertIn('data === "cmd:short"', self.text)
        self.assertIn("بدأ بحث الشورت", self.text)
        self.assertIn("statefulCallbackAck(target.data)", self.text)
        self.assertNotIn('answerCallback(env, target.callbackId, "⚡ تم الاستلام")', self.text)

    def test_retry_is_not_added_to_v1_surface(self):
        self.assertNotIn("cmd:retry", self.text)
        self.assertNotIn("retry:", self.text)


if __name__ == "__main__":
    unittest.main()

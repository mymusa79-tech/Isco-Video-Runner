from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EDGE = ROOT / "cloudflare" / "telegram-control-worker" / "observability-worker.js"
CORE = ROOT / "cloudflare" / "telegram-control-worker" / "observability-worker-v4-core.js"


class CreatorControlCenterV5EdgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = EDGE.read_text(encoding="utf-8")
        cls.core = CORE.read_text(encoding="utf-8")

    def test_v5_is_live_wrapper_over_previous_read_only_core(self):
        self.assertIn('import priorWorker from "./observability-worker-v4-core.js"', self.text)
        self.assertIn("return priorWorker.fetch(request, env, ctx)", self.text)
        self.assertIn('import baseWorker from "./index.js"', self.core)

    def test_compact_control_center_uses_edit_in_place(self):
        self.assertIn("مركز التحكم", self.text)
        self.assertIn('callback_data: "cmd:search_menu"', self.text)
        self.assertIn('callback_data: "cmd:library_menu"', self.text)
        self.assertIn('callback_data: "cmd:last_delivery"', self.text)
        self.assertIn('callback_data: "cmd:stats_menu"', self.text)
        self.assertIn('telegram(env, "editMessageText"', self.text)
        self.assertIn("message is not modified", self.text)

    def test_stats_are_dashboard_first_and_honest_about_unavailable_analytics(self):
        for marker in (
            "🌐 نظرة عامة",
            "🎬 آخر فيديو",
            "⚡ آخر Short",
            "تفاعل ظاهر",
            "إعجاب + تعليق ÷ مشاهدة",
            "CTR والاحتفاظ ومدة المشاهدة",
            "لا تدّعي عدد المشاهدات المكتسبة داخل الفترة",
        ):
            self.assertIn(marker, self.text)
        self.assertIn("duration <= 180", self.text)

    def test_status_is_operator_first_with_technical_details_separated(self):
        self.assertIn("الحالة — ماذا يحدث الآن؟", self.text)
        self.assertIn("مطلوب منك", self.text)
        self.assertIn("تأكيد الإنتاج", self.text)
        self.assertIn('callback_data: "cmd:system_status"', self.text)
        self.assertIn("هذه شاشة تشخيص فقط", self.text)

    def test_delivery_shows_latest_long_and_short_separately(self):
        self.assertIn('latestByPrefix(items, "video-")', self.text)
        self.assertIn('latestByPrefix(items, "short-")', self.text)
        self.assertIn("🎬 آخر حلقة", self.text)
        self.assertIn("⚡ آخر Short", self.text)
        self.assertIn("🎬 حزمة الحلقة", self.text)
        self.assertIn("⚡ حزمة الشورت", self.text)

    def test_v5_wrapper_cannot_dispatch_or_start_production(self):
        self.assertNotIn("dispatchToGitHub", self.text)
        self.assertNotIn("workflow_dispatch", self.text)
        self.assertNotIn("telegram-production-request.yml", self.text)
        self.assertNotIn("produce-resilient-v4.yml", self.text)
        self.assertNotIn("cmd:retry", self.text)
        self.assertNotIn("cmd:produce_latest", self.text)

    def test_exact_identity_and_webhook_secret_boundaries_remain_before_v5_routes(self):
        for marker in (
            "X-Telegram-Bot-Api-Secret-Token",
            "TELEGRAM_WEBHOOK_SECRET",
            "TELEGRAM_ALLOWED_USER_ID",
            "TELEGRAM_CHAT_ID",
            "secretHeaderValid(request, env)",
            "authorized(update, env)",
        ):
            self.assertIn(marker, self.text)

    def test_javascript_is_valid_when_node_is_available(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is unavailable")
        subprocess.run([node, "--check", str(EDGE)], check=True, capture_output=True, text=True)
        subprocess.run([node, "--check", str(CORE)], check=True, capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from scripts import telegram_topic_memory_ui as memory_ui


ROOT = Path(__file__).resolve().parents[1]
WEBHOOK_REPLAY = ROOT / "scripts" / "telegram_webhook_replay.py"


class TelegramResearchSelectionFlowTests(unittest.TestCase):
    def tearDown(self):
        memory_ui._TERMINAL_QUOTA_MESSAGE = None
        memory_ui._RESEARCH_CONTEXT = None

    def test_webhook_replay_is_directly_executable_from_repository_root(self):
        result = subprocess.run(
            [sys.executable, str(WEBHOOK_REPLAY), "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("ModuleNotFoundError", result.stderr)
        self.assertIn("telegram edge-webhook bridge", result.stdout.casefold())

    def test_numbered_research_buttons_are_explicit_actions(self):
        rows = memory_ui._clear_candidate_keyboard("session-1", "long")
        self.assertEqual(rows[0][0]["text"], "✅ 1️⃣ اختر هذه الفكرة")
        self.assertEqual(rows[1][0]["text"], "✅ 2️⃣ اختر هذه الفكرة")
        self.assertEqual(rows[2][0]["text"], "✅ 3️⃣ اختر هذه الفكرة")
        self.assertEqual(rows[0][0]["callback_data"], "pick:session-1:0")
        self.assertEqual(rows[0][0]["style"], "success")
        self.assertEqual(rows[0][1]["text"], "📋 تفاصيل الفكرة 1")

    def test_research_panel_explains_what_selection_does(self):
        candidates = [
            {"title": "الفكرة الأولى", "control_score": 0.81, "why": ["سبب واضح"]},
            {"title": "الفكرة الثانية", "control_score": 0.77, "why": []},
            {"title": "الفكرة الثالثة", "control_score": 0.72, "why": []},
        ]
        text = memory_ui._clear_candidate_panel_text("long", candidates)
        self.assertIn("طريقة الاختيار", text)
        self.assertIn("اختر الفكرة", text)
        self.assertIn("الاختيار وحده لا يبدأ Production", text)

    def test_retry_after_uses_provider_advised_delay_only(self):
        exc = RuntimeError("429 RESOURCE_EXHAUSTED. Please retry in 43.06502384s.")
        self.assertAlmostEqual(memory_ui._retry_after_seconds(exc), 43.06502384)
        self.assertIsNone(memory_ui._retry_after_seconds(RuntimeError("429 quota exceeded")))
        self.assertIsNone(memory_ui._retry_after_seconds(RuntimeError("Please retry in 600s")))

    def test_transient_free_quota_is_retried_with_bounded_wait(self):
        calls = {"count": 0}

        def original(*_args, **_kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("429 RESOURCE_EXHAUSTED. Please retry in 2.5s.")
            return {0: "self efficacy", 1: "procrastination", 2: "social comparison"}

        with mock.patch.object(memory_ui.ui.simple, "_ORIGINAL_ENGLISH_RESEARCH_QUERIES", side_effect=original, create=True), \
             mock.patch.object(memory_ui.time, "sleep") as sleep, \
             mock.patch.object(memory_ui, "_notify_research") as notice:
            result = memory_ui._resilient_english_research_queries("key", [{}, {}, {}], "model")

        self.assertEqual(calls["count"], 2)
        self.assertEqual(len(result), 3)
        sleep.assert_called_once()
        self.assertLessEqual(float(sleep.call_args.args[0]), memory_ui.MAX_TRANSIENT_QUOTA_WAIT_SECONDS)
        notice.assert_called_once()

    def test_daily_or_unbounded_quota_fails_closed_without_paid_fallback(self):
        def exhausted(*_args, **_kwargs):
            raise RuntimeError("429 RESOURCE_EXHAUSTED: free tier quota exceeded")

        with mock.patch.object(memory_ui.ui.simple, "_ORIGINAL_ENGLISH_RESEARCH_QUERIES", side_effect=exhausted, create=True), \
             mock.patch.object(memory_ui.time, "sleep") as sleep:
            with self.assertRaises(RuntimeError):
                memory_ui._resilient_english_research_queries("key", [{}, {}, {}], "model")
        sleep.assert_not_called()
        self.assertIn("حصة Gemini المجانية", memory_ui._TERMINAL_QUOTA_MESSAGE or "")
        self.assertIn("لم أستخدم أي مسار مدفوع", memory_ui._TERMINAL_QUOTA_MESSAGE or "")

    def test_detail_and_scope_back_buttons_return_to_same_session(self):
        detail_keyboard = [
            [{"text": "✅ اختيار 1", "callback_data": "pick:abc123:0"}],
            [{"text": "↩️ الخيارات", "callback_data": "cmd:menu"}],
        ]
        rewritten = memory_ui._rewrite_contextual_keyboard(detail_keyboard)
        self.assertEqual(rewritten[1][0]["text"], "↩️ نفس الخيارات")
        self.assertEqual(rewritten[1][0]["callback_data"], "cmd:choices-abc123")

        scope_keyboard = [
            [{"text": "🎬 حلقة + Shorts", "callback_data": "scope:abc123:0:bundle"}],
            [{"text": "🎬 حلقة فقط", "callback_data": "scope:abc123:0:long"}],
            [{"text": "↩️ اللوحة", "callback_data": "cmd:menu"}],
        ]
        rewritten = memory_ui._rewrite_contextual_keyboard(scope_keyboard)
        self.assertEqual(rewritten[2][0]["callback_data"], "cmd:choices-abc123")


if __name__ == "__main__":
    unittest.main()

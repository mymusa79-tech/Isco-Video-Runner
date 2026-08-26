from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

from scripts import telegram_topic_memory_ui as memory_ui


ROOT = Path(__file__).resolve().parents[1]
WEBHOOK_REPLAY = ROOT / "scripts" / "telegram_webhook_replay.py"


class TelegramResearchSelectionFlowTests(unittest.TestCase):
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
        self.assertEqual(rows[0][1]["text"], "📋 تفاصيل الفكرة 1")

    def test_research_panel_explains_what_selection_does(self):
        candidates = [
            {"title": "الفكرة الأولى", "control_score": 0.81, "why": ["سبب واضح"]},
            {"title": "الفكرة الثانية", "control_score": 0.77, "why": []},
            {"title": "الفكرة الثالثة", "control_score": 0.72, "why": []},
        ]
        text = memory_ui._clear_candidate_panel_text("long", candidates)
        self.assertIn("طريقة الاختيار واضحة", text)
        self.assertIn("اختر هذه الفكرة", text)
        self.assertIn("لا يبدأ Production بمجرد الاختيار", text)

    def test_long_research_start_message_explains_next_action(self):
        text = memory_ui._research_started_text("topic")
        self.assertIn("بدأ بحث الحلقة", text)
        self.assertIn("3 أفكار مرقمة", text)
        self.assertIn("اختر هذه الفكرة", text)
        self.assertIn("تفاصيل الفكرة", text)
        self.assertIn("لا يبدأ Production", text)

    def test_short_research_start_message_explains_next_action(self):
        text = memory_ui._research_started_text("short")
        self.assertIn("بدأ بحث الشورت", text)
        self.assertIn("3 أفكار مرقمة", text)
        self.assertIn("لا يبدأ Production", text)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
from unittest import mock

from scripts import telegram_creator_control_center_v5 as v5


class _Client:
    def __init__(self):
        self.messages = []

    def send(self, chat_id, text, *, keyboard=None):
        self.messages.append((chat_id, text, keyboard))


class _Releases:
    def __init__(self):
        self.items = [
            {
                "tag_name": "video-abc123",
                "name": "كيف تنهض بعد أن تسقط كثيرًا؟",
                "published_at": "2026-08-30T18:00:00Z",
                "html_url": "https://github.com/x/releases/tag/video-abc123",
                "draft": False,
            },
            {
                "tag_name": "short-def456",
                "name": "Short — لا تنتظر الدافع",
                "published_at": "2026-08-30T19:00:00Z",
                "html_url": "https://github.com/x/releases/tag/short-def456",
                "draft": False,
            },
        ]

    def releases(self):
        return list(self.items)


class CreatorControlCenterV5Tests(unittest.TestCase):
    def test_root_is_compact_action_first_and_has_no_production_button(self):
        rows = v5._main_keyboard()
        self.assertEqual(len(rows), 3)
        self.assertEqual([button["callback_data"] for button in rows[0]], ["cmd:search_menu", "cmd:library_menu"])
        self.assertEqual([button["callback_data"] for button in rows[1]], ["cmd:last_delivery", "cmd:stats_menu"])
        callbacks = [button.get("callback_data") for row in rows for button in row]
        self.assertNotIn("cmd:produce_latest", callbacks)
        self.assertIn("مركز التحكم", v5._menu_text())

    def test_library_overview_separates_long_and_short_counts(self):
        state = {
            "saved_suggestions": [
                {"archive_id": "a", "status": "available", "kind": "long", "candidate": {"title": "حلقة"}, "last_seen_at": "2026-08-30T00:00:00Z"},
                {"archive_id": "b", "status": "available", "kind": "short", "candidate": {"title": "شورت"}, "last_seen_at": "2026-08-30T00:00:00Z"},
            ],
            "used_topics": [
                {"request_id": "r1", "kind": "short", "topic": "مستعمل", "used_at": "2026-08-30T00:00:00Z"},
            ],
        }
        text, rows = v5._library_overview(state)
        self.assertIn("🎬 1 حلقات", text)
        self.assertIn("⚡ 1 Shorts", text)
        self.assertIn("✅ المستعملة (1)", rows[0][1]["text"])

    def test_candidate_panel_is_decision_card_not_debug_dump(self):
        candidates = [
            {
                "title": "الفكرة الأولى",
                "control_score": 0.87,
                "channel_fit_score": 0.93,
                "trend_score": 0.71,
                "market_class": "rising",
                "evidence_quality": 0.80,
                "why": ["Hook قوي", "سبب ثان"],
            },
            {
                "title": "الفكرة الثانية",
                "control_score": 0.82,
                "channel_fit_score": 0.90,
                "trend_score": 0.50,
                "market_class": "hybrid",
                "evidence_quality": 0.80,
                "why": ["ملاءمة قوية"],
            },
            {
                "title": "الفكرة الثالثة",
                "control_score": 0.78,
                "channel_fit_score": 0.91,
                "trend_score": 0.30,
                "market_class": "evergreen",
                "evidence_quality": 0.80,
                "why": ["قيمة مستمرة"],
            },
        ]
        text = v5._candidate_panel_text("long", candidates)
        self.assertIn("⭐ الأنسب الآن", text)
        self.assertIn("🎯 القناة", text)
        self.assertIn("📈 الآن", text)
        self.assertIn("الزخم الحالي محدود", text)
        self.assertNotIn("أعلى سرعة مشاهدة", text)
        self.assertNotIn("عينات صالحة", text)

    def test_confirmation_helper_only_copies_exact_phrase_and_never_executes(self):
        rows = v5._approval_copy_rows(
            [[{"text": "🏠 الرئيسية", "callback_data": "cmd:menu"}]],
            "✅ تم اعتماد القرار\nاكتب حرفيًا: تأكيد الإنتاج",
        )
        self.assertEqual(rows[0][0]["copy_text"]["text"], "تأكيد الإنتاج")
        self.assertNotIn("callback_data", rows[0][0])
        self.assertEqual(rows[1][0]["callback_data"], "cmd:menu")

    def test_detail_back_returns_to_exact_candidate_session(self):
        rows = [[{"text": "↩️ الخيارات", "callback_data": "cmd:menu"}]]
        fixed = v5._detail_back_rows(rows, {"data": "detail:session-7:1"})
        self.assertEqual(fixed[0][0]["callback_data"], "cmd:choices-session-7")

    def test_operator_status_prioritizes_user_action_over_system_noise(self):
        state = {
            "requests": {
                "req-1": {"approved_topic": "موضوع معتمد"},
            },
            "active_research_session_id": "s1",
            "production_target": {"request_id": "req-1", "request_sha256": "x", "session_id": "s1"},
            "pending_actions": [],
            "production_queue": [],
        }
        with mock.patch.object(v5.active, "_production_enabled", return_value=True):
            text, rows = v5._operator_status(state, _Releases())
        self.assertIn("مطلوب منك", text)
        self.assertIn("موضوع معتمد", text)
        self.assertIn("تأكيد الإنتاج", text)
        self.assertEqual(rows[0][0]["callback_data"], "cmd:system_status")

    def test_pending_research_status_does_not_ask_for_production(self):
        state = {"pending_actions": [{"kind": "short", "status": "pending"}], "production_queue": []}
        text, _ = v5._operator_status(state, _Releases())
        self.assertIn("بحث الشورت", text)
        self.assertIn("انتظر ظهور 3 الخيارات", text)
        self.assertNotIn("إذا كان القرار نهائيًا", text)

    def test_last_delivery_surfaces_long_and_short_names_not_tags(self):
        text, rows = v5._last_delivery({}, _Releases())
        self.assertIn("كيف تنهض بعد أن تسقط كثيرًا؟", text)
        self.assertIn("Short — لا تنتظر الدافع", text)
        self.assertNotIn("video-abc123", text)
        urls = [button.get("url") for row in rows for button in row if button.get("url")]
        self.assertEqual(len(urls), 2)

    def test_research_queue_is_single_and_clears_stale_selection(self):
        client = _Client()
        state = {"production_target": {"request_id": "old"}, "active_research_session_id": "old", "pending_actions": []}
        with mock.patch.object(v5.panel, "_queue_research", return_value=True) as queue, \
             mock.patch.object(v5.active, "_clear_current_selection") as clear:
            v5._queue_research("topic", client, state, 77)
        clear.assert_called_once_with(state)
        queue.assert_called_once_with(state, "long", 77)
        self.assertIn("لا يبدأ Production", client.messages[-1][1])


if __name__ == "__main__":
    unittest.main()

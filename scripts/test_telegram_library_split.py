from __future__ import annotations

import unittest

from scripts import telegram_control_active_ui as ui
from scripts import telegram_topic_memory_ui as memory


def _saved(archive_id: str, kind: str, title: str) -> dict:
    return {
        "schema_version": 1,
        "archive_id": archive_id,
        "status": "available",
        "kind": kind,
        "dedupe_key": ui._suggestion_key(kind, title),
        "saved_at": "2026-08-28T00:00:00+00:00",
        "last_seen_at": "2026-08-28T00:00:00+00:00",
        "candidate": {"title": title, "control_score": 0.8},
    }


def _used(request_id: str, kind: str, topic: str) -> dict:
    return {
        "schema_version": 1,
        "request_id": request_id,
        "kind": kind,
        "topic": topic,
        "dedupe_key": ui._suggestion_key(kind, topic),
        "approval_scope": "short_only" if kind == "short" else "long_only",
        "release_tag": f"{'short' if kind == 'short' else 'video'}-telegram-{request_id}",
        "used_at": "2026-08-28T00:00:00+00:00",
    }


class TelegramLibrarySplitTests(unittest.TestCase):
    def test_saved_menu_has_independent_long_and_short_counts(self):
        state = {
            ui.SAVED_SUGGESTIONS_KEY: [
                _saved("l1", "long", "حلقة أولى"),
                _saved("l2", "long", "حلقة ثانية"),
                _saved("s1", "short", "شورت أول"),
            ]
        }
        text, keyboard = memory._saved_kind_menu(state)
        self.assertIn("🎬 طويل — 2", text)
        self.assertIn("⚡ شورت — 1", text)
        self.assertEqual(keyboard[0][0]["callback_data"], "cmd:saved-long")
        self.assertEqual(keyboard[1][0]["callback_data"], "cmd:saved-short")

    def test_saved_long_page_never_mixes_shorts(self):
        state = {
            ui.SAVED_SUGGESTIONS_KEY: [
                _saved("l1", "long", "موضوع طويل فقط"),
                _saved("s1", "short", "موضوع شورت فقط"),
            ]
        }
        text, keyboard = memory._saved_page_by_kind(state, "long", 0)
        self.assertIn("موضوع طويل فقط", keyboard[0][0]["text"])
        self.assertNotIn("موضوع شورت فقط", text)
        self.assertFalse(any("موضوع شورت فقط" in button["text"] for row in keyboard for button in row))
        self.assertEqual(keyboard[-1][0]["callback_data"], "cmd:saved")

    def test_used_menu_has_independent_long_and_short_counts(self):
        state = {
            ui.USED_TOPICS_KEY: [
                _used("r1", "long", "حلقة منتجة"),
                _used("r2", "short", "شورت منتج"),
                _used("r3", "short", "شورت منتج ثان"),
            ]
        }
        text, keyboard = memory._used_kind_menu(state)
        self.assertIn("🎬 طويل — 1", text)
        self.assertIn("⚡ شورت — 2", text)
        self.assertEqual(keyboard[0][0]["callback_data"], "cmd:used-long")
        self.assertEqual(keyboard[1][0]["callback_data"], "cmd:used-short")

    def test_used_short_page_never_mixes_long_topics(self):
        state = {
            ui.USED_TOPICS_KEY: [
                _used("r1", "long", "حلقة قديمة"),
                _used("r2", "short", "شورت قديم"),
            ]
        }
        text, keyboard = memory._used_page_by_kind(state, "short", 0)
        self.assertIn("شورت قديم", text)
        self.assertNotIn("حلقة قديمة", text)
        self.assertEqual(keyboard[-1][0]["callback_data"], "cmd:used")

    def test_page_callback_parser_keeps_format_and_page(self):
        self.assertEqual(memory._library_page_request("saved-long", "saved"), ("long", 0))
        self.assertEqual(memory._library_page_request("saved-short-page-3", "saved"), ("short", 3))
        self.assertEqual(memory._library_page_request("used-long-page-2", "used"), ("long", 2))
        self.assertIsNone(memory._library_page_request("saved-long", "used"))


if __name__ == "__main__":
    unittest.main()

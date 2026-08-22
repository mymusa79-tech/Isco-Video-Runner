from __future__ import annotations

import unittest

from scripts import telegram_control_simple_ui as ui


class SimpleTelegramUiTests(unittest.TestCase):
    def test_main_surface_has_exactly_three_entries(self):
        keyboard = ui._main_keyboard()
        labels = [button[0]["text"] for button in keyboard]
        self.assertEqual(labels, ["✨ اقترح", "🎁 آخر إنتاج", "🧭 الحالة"])

    def test_main_surface_has_no_publish_upload_or_schedule_action(self):
        payload = str(ui._main_keyboard()) + ui._menu_text()
        lowered = payload.casefold()
        self.assertNotIn("publish", lowered)
        self.assertNotIn("upload", lowered)
        self.assertNotIn("schedule", lowered)
        self.assertIn("لا نشر", payload)
        self.assertIn("يدوي", payload)

    def test_delivery_view_combines_long_and_short(self):
        long_release = {"tag_name": "video-10", "published_at": "2026-08-22T10:00:00Z", "html_url": "https://example.com/long"}
        short_release = {"tag_name": "short-9", "published_at": "2026-08-22T11:00:00Z", "html_url": "https://example.com/short"}
        text = ui._last_delivery_text(long_release, short_release)
        self.assertIn("video-10", text)
        self.assertIn("short-9", text)
        self.assertIn("نشر يدوي", text)
        buttons = [button for row in ui._delivery_keyboard(long_release, short_release) for button in row]
        labels = " ".join(button["text"] for button in buttons)
        self.assertIn("حزمة الفيديو", labels)
        self.assertIn("آخر شورت", labels)
        self.assertIn("A/B/C", labels)

    def test_suggest_surface_asks_only_content_type(self):
        class FakeClient:
            def __init__(self):
                self.calls = []
            def send(self, chat_id, text, *, keyboard=None):
                self.calls.append((chat_id, text, keyboard))

        fake = FakeClient()
        ui._handle_command("suggest", fake, {}, None, 123)
        _, text, keyboard = fake.calls[0]
        self.assertIn("ماذا تريد", text)
        labels = [row[0]["text"] for row in keyboard]
        self.assertEqual(labels[:2], ["🎬 حلقة", "📱 شورت"])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import unittest

from scripts import telegram_bot_api_10_3_ui as rich_ui


class TelegramBotApi103UiTests(unittest.TestCase):
    def _candidates(self):
        return [
            {
                "title": f"الفكرة {index + 1}",
                "control_score": 0.82 - index * 0.04,
                "why": ["ملاءمة قوية", "احتفاظ جيد"],
                "audience_fit": 0.8,
                "hook_potential": 0.78,
                "retention_potential": 0.76,
            }
            for index in range(3)
        ]

    def _keyboard(self, kind="long"):
        pick = "pickshort" if kind == "short" else "pick"
        return [
            [
                {"text": f"✅ {index + 1}", "callback_data": f"{pick}:session-1:{index}"},
                {"text": f"📋 {index + 1}", "callback_data": f"detail:session-1:{index}"},
            ]
            for index in range(3)
        ] + [
            [{"text": "🔄", "callback_data": f"refresh:{kind}"}],
            [{"text": "🏠", "callback_data": "cmd:menu"}],
        ]

    def test_rich_candidate_card_is_rtl_and_embeds_expandable_details(self):
        message = rich_ui._candidate_rich_message("long", self._candidates(), self._keyboard())
        self.assertTrue(message["is_rtl"])
        self.assertTrue(message["skip_entity_detection"])
        block_types = [block["type"] for block in message["blocks"]]
        self.assertEqual(block_types.count("details"), 3)
        self.assertGreaterEqual(block_types.count("buttons"), 4)
        self.assertIn("footer", block_types)

    def test_each_rich_selection_button_keeps_the_exact_session_bound_callback(self):
        message = rich_ui._candidate_rich_message("long", self._candidates(), self._keyboard())
        callbacks = [
            button.get("callback_data")
            for block in message["blocks"]
            if block.get("type") == "buttons"
            for button in block.get("buttons", [])
        ]
        for index in range(3):
            self.assertIn(f"pick:session-1:{index}", callbacks)
        self.assertIn("refresh:long", callbacks)
        self.assertIn("cmd:menu", callbacks)

    def test_short_rich_selection_uses_short_only_callback_family(self):
        message = rich_ui._candidate_rich_message("short", self._candidates(), self._keyboard("short"))
        payload = json.dumps(message, ensure_ascii=False)
        self.assertIn("pickshort:session-1:0", payload)
        self.assertNotIn('"callback_data": "pick:session-1:', payload)

    def test_research_busy_surface_uses_bot_api_10_3_disabled_button(self):
        keyboard = rich_ui._research_busy_keyboard()
        self.assertEqual(keyboard[0][0]["text"], "⏳ البحث جارٍ")
        self.assertEqual(keyboard[0][0]["disabled"], {})
        self.assertNotIn("callback_data", keyboard[0][0])
        self.assertEqual(keyboard[1][0]["callback_data"], "cmd:menu")

    def test_callback_context_is_single_use(self):
        class Client:
            pass

        client = Client()
        setattr(client, rich_ui._CALLBACK_CONTEXT_ATTR, "cb-123")
        self.assertEqual(rich_ui.consume_callback_query_id(client), "cb-123")
        self.assertIsNone(rich_ui.consume_callback_query_id(client))

    def test_rich_surface_never_adds_a_production_callback(self):
        message = rich_ui._candidate_rich_message("long", self._candidates(), self._keyboard())
        callbacks = [
            str(button.get("callback_data") or "")
            for block in message["blocks"]
            if block.get("type") == "buttons"
            for button in block.get("buttons", [])
        ]
        self.assertFalse(any("produce" in value or "production" in value or "confirm" in value for value in callbacks))


if __name__ == "__main__":
    unittest.main()

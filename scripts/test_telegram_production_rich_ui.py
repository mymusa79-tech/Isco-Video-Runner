from __future__ import annotations

import json
import unittest

from scripts import telegram_production_rich_ui as rich


class TelegramProductionRichUiTests(unittest.TestCase):
    def test_status_has_disabled_current_stage_and_refresh(self):
        message = rich.production_status_rich_message({"stage": "editing", "progress": 63, "run_id": "116"})
        payload = json.dumps(message, ensure_ascii=False)
        self.assertIn("المونتاج", payload)
        self.assertIn('"disabled": {}', payload)
        self.assertIn('"callback_data": "cmd:status"', payload)

    def test_quality_gates_surface_failure_details_without_repair_callback(self):
        message = rich.quality_gates_rich_message(
            {
                "run_id": "116",
                "gates": [
                    {"name": "Arabic", "passed": True},
                    {"name": "Render", "passed": False, "reason": "missing frame"},
                ],
            }
        )
        payload = json.dumps(message, ensure_ascii=False)
        self.assertIn("missing frame", payload)
        self.assertNotIn("repair", payload.casefold())
        self.assertNotIn("produce", payload.casefold())

    def test_last_delivery_embeds_input_media_documents(self):
        message = rich.last_delivery_rich_message(
            {
                "title": "حلقة",
                "files": [
                    {"name": "script.txt", "telegram_file_id": "BQACAgQAAxkBAAIB"},
                    {"name": "final.mp4", "browser_download_url": "https://example.invalid/final.mp4"},
                ],
            }
        )
        documents = [block for block in message["blocks"] if block.get("type") == "document"]
        self.assertEqual(len(documents), 2)
        self.assertEqual(documents[0]["document"], {"type": "document", "media": "BQACAgQAAxkBAAIB"})
        self.assertEqual(documents[1]["document"]["media"], "https://example.invalid/final.mp4")

    def test_ephemeral_replaces_callback_message_for_exact_receiver(self):
        self.assertEqual(
            rich.ephemeral_callback_parameters("cb-1", "12345"),
            {"receiver_user_id": 12345, "callback_query_id": "cb-1", "replace_callback_query_message": True},
        )
        self.assertIsNone(rich.ephemeral_callback_parameters("cb-1", None))


if __name__ == "__main__":
    unittest.main()

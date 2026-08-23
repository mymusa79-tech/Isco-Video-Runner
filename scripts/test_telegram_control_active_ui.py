from __future__ import annotations

import os
import unittest
from unittest import mock

from scripts import telegram_control_active_ui as ui
from scripts import telegram_production_queue as queue


class _Client:
    def __init__(self):
        self.messages = []

    def send(self, chat_id, text, *, keyboard=None):
        self.messages.append((chat_id, text, keyboard))


def _request() -> dict:
    item = {
        "schema_version": 1,
        "request_id": "req-ui-1",
        "source": "telegram_editorial_control_panel",
        "kind": "long",
        "approval_scope": "long_only",
        "approved_topic": "موضوع معتمد",
        "approved_at": "2026-08-23T07:00:00+00:00",
        "approved_by_user": True,
        "production_dispatch_authorized": False,
        "status": "approved_waiting_production_activation",
    }
    item["request_sha256"] = queue._request_hash(item)
    return item


class TelegramActiveUiTests(unittest.TestCase):
    def test_enabled_surface_has_one_explicit_start_button(self):
        with mock.patch.dict(os.environ, {"CONTROL_PLANE_PRODUCTION_ENABLED": "true"}, clear=False):
            buttons = [button for row in ui._main_keyboard() for button in row]
            starts = [button for button in buttons if button.get("callback_data") == "cmd:produce_latest"]
            self.assertEqual(len(starts), 1)
            self.assertIn("🚀", starts[0]["text"])
            menu = ui._menu_text()
            self.assertIn("إنتاج Telegram مفعّل", menu)
            self.assertIn("ضغطة مستقلة", menu)
            self.assertIn("لا نشر إلى YouTube", menu)

    def test_approval_never_auto_starts_production(self):
        with mock.patch.dict(os.environ, {"CONTROL_PLANE_PRODUCTION_ENABLED": "true"}, clear=False):
            text = ui._approval_text(_request())
            self.assertIn("لم يبدأ الإنتاج بعد", text)
            self.assertIn("ابدأ الإنتاج المعتمد", text)

    def test_start_button_queues_exact_latest_approved_request(self):
        state = {"requests": {"req-ui-1": _request()}, "production_queue": [], "last_event_at": None}
        client = _Client()
        with mock.patch.dict(os.environ, {"CONTROL_PLANE_PRODUCTION_ENABLED": "true"}, clear=False):
            ui._handle_command("produce_latest", client, state, None, 77)
        self.assertEqual(len(state["production_queue"]), 1)
        action = state["production_queue"][0]
        self.assertEqual(action["request_id"], "req-ui-1")
        self.assertEqual(action["status"], "pending_dispatch")
        self.assertFalse(state["requests"]["req-ui-1"]["production_dispatch_authorized"])
        self.assertIn("تم تأكيد بدء الإنتاج", client.messages[-1][1])

    def test_disabled_surface_cannot_queue_production(self):
        state = {"requests": {"req-ui-1": _request()}, "production_queue": []}
        client = _Client()
        with mock.patch.dict(os.environ, {"CONTROL_PLANE_PRODUCTION_ENABLED": "false"}, clear=False):
            ui._handle_command("produce_latest", client, state, None, 77)
            buttons = [button for row in ui._main_keyboard() for button in row]
        self.assertEqual(state["production_queue"], [])
        self.assertFalse(any(button.get("callback_data") == "cmd:produce_latest" for button in buttons))
        self.assertIn("مقفول", client.messages[-1][1])


if __name__ == "__main__":
    unittest.main()

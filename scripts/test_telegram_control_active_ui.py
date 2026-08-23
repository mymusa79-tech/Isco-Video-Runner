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


def _request(request_id: str = "req-ui-1", *, approved_at: str = "2026-08-23T07:00:00+00:00") -> dict:
    item = {
        "schema_version": 1,
        "request_id": request_id,
        "source": "telegram_editorial_control_panel",
        "kind": "long",
        "approval_scope": "long_only",
        "approved_topic": f"موضوع معتمد {request_id}",
        "approved_at": approved_at,
        "approved_by_user": True,
        "production_dispatch_authorized": False,
        "status": "approved_waiting_production_activation",
    }
    item["request_sha256"] = queue._request_hash(item)
    return item


def _target(request: dict, session_id: str = "session-current") -> dict:
    return {
        "request_id": request["request_id"],
        "request_sha256": request["request_sha256"],
        "session_id": session_id,
        "selected_at": request["approved_at"],
    }


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

    def test_old_saved_approval_cannot_start_without_current_research_target(self):
        old = _request("req-old")
        state = {"requests": {old["request_id"]: old}, "production_queue": [], "last_event_at": None}
        client = _Client()
        with mock.patch.dict(os.environ, {"CONTROL_PLANE_PRODUCTION_ENABLED": "true"}, clear=False):
            ui._handle_command("produce_latest", client, state, None, 77)
        self.assertEqual(state["production_queue"], [])
        self.assertIn("جلسة البحث الحالية", client.messages[-1][1])

    def test_start_button_queues_exact_current_selection_not_latest_saved_request(self):
        selected = _request("req-selected", approved_at="2026-08-23T07:00:00+00:00")
        newer_saved = _request("req-newer-saved", approved_at="2026-08-23T08:00:00+00:00")
        state = {
            "requests": {
                selected["request_id"]: selected,
                newer_saved["request_id"]: newer_saved,
            },
            "production_queue": [],
            "last_event_at": None,
            ui.ACTIVE_RESEARCH_SESSION_KEY: "session-current",
            ui.PRODUCTION_TARGET_KEY: _target(selected),
        }
        client = _Client()
        with mock.patch.dict(os.environ, {"CONTROL_PLANE_PRODUCTION_ENABLED": "true"}, clear=False):
            ui._handle_command("produce_latest", client, state, None, 77)
        self.assertEqual(len(state["production_queue"]), 1)
        action = state["production_queue"][0]
        self.assertEqual(action["request_id"], "req-selected")
        self.assertEqual(action["request_sha256"], selected["request_sha256"])
        self.assertFalse(selected["production_dispatch_authorized"])
        self.assertIn("تم تأكيد بدء الإنتاج", client.messages[-1][1])

    def test_starting_new_research_invalidates_previous_production_target(self):
        selected = _request("req-selected")
        state = {
            "requests": {selected["request_id"]: selected},
            "production_queue": [],
            "pending_actions": [],
            ui.ACTIVE_RESEARCH_SESSION_KEY: "session-old",
            ui.PRODUCTION_TARGET_KEY: _target(selected, "session-old"),
        }
        client = _Client()
        with mock.patch.object(ui.simple, "_handle_command") as delegated:
            ui._handle_command("topic", client, state, None, 77)
        self.assertNotIn(ui.PRODUCTION_TARGET_KEY, state)
        self.assertNotIn(ui.ACTIVE_RESEARCH_SESSION_KEY, state)
        delegated.assert_called_once_with("topic", client, state, None, 77)

    def test_approval_binds_only_the_active_research_session(self):
        request = _request("req-selected")
        session = {"session_id": "session-current", "kind": "long", "candidates": []}
        state = {
            "requests": {},
            "production_queue": [],
            ui.ACTIVE_RESEARCH_SESSION_KEY: "session-current",
        }
        with mock.patch.object(ui.simple, "_approve", return_value=request):
            approved = ui._approve_current(state, session, 0, "long")
        self.assertIs(approved, request)
        self.assertEqual(state[ui.PRODUCTION_TARGET_KEY]["request_id"], "req-selected")
        self.assertEqual(state[ui.PRODUCTION_TARGET_KEY]["request_sha256"], request["request_sha256"])
        self.assertEqual(state[ui.PRODUCTION_TARGET_KEY]["session_id"], "session-current")

    def test_stale_research_session_cannot_become_production_target(self):
        state = {ui.ACTIVE_RESEARCH_SESSION_KEY: "session-new"}
        stale = {"session_id": "session-old", "kind": "long", "candidates": []}
        with mock.patch.object(ui.simple, "_approve") as approve:
            with self.assertRaisesRegex(RuntimeError, "current research session"):
                ui._approve_current(state, stale, 0, "long")
        approve.assert_not_called()
        self.assertNotIn(ui.PRODUCTION_TARGET_KEY, state)

    def test_disabled_surface_cannot_queue_production(self):
        selected = _request()
        state = {
            "requests": {selected["request_id"]: selected},
            "production_queue": [],
            ui.ACTIVE_RESEARCH_SESSION_KEY: "session-current",
            ui.PRODUCTION_TARGET_KEY: _target(selected),
        }
        client = _Client()
        with mock.patch.dict(os.environ, {"CONTROL_PLANE_PRODUCTION_ENABLED": "false"}, clear=False):
            ui._handle_command("produce_latest", client, state, None, 77)
            buttons = [button for row in ui._main_keyboard() for button in row]
        self.assertEqual(state["production_queue"], [])
        self.assertFalse(any(button.get("callback_data") == "cmd:produce_latest" for button in buttons))
        self.assertIn("مقفول", client.messages[-1][1])


if __name__ == "__main__":
    unittest.main()

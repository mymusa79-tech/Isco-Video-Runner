from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import telegram_control_active_ui as active
from scripts import telegram_control_panel as panel
from scripts import telegram_persistent_control_ui as ui
from scripts import telegram_production_queue as queue


class _Client:
    def __init__(self):
        self.messages = []
        self.calls = []

    def send(self, chat_id, text, *, keyboard=None):
        self.messages.append((chat_id, text, keyboard))

    def call(self, method, payload):
        self.calls.append((method, payload))
        return {"message_id": 123}


def _request(request_id: str = "req-persistent-1") -> dict:
    request = {
        "schema_version": 1,
        "request_id": request_id,
        "source": "telegram_editorial_control_panel",
        "kind": "long",
        "approval_scope": "long_only",
        "approved_topic": "موضوع محفوظ ومعتمد",
        "approved_at": "2026-08-26T06:00:00+00:00",
        "approved_by_user": True,
        "production_dispatch_authorized": False,
        "status": "approved_waiting_production_activation",
    }
    request["request_sha256"] = queue._request_hash(request)
    return request


def _target(request: dict, session_id: str = "session-current") -> dict:
    return {
        "request_id": request["request_id"],
        "request_sha256": request["request_sha256"],
        "session_id": session_id,
        "selected_at": request["approved_at"],
    }


class TelegramPersistentControlUiTests(unittest.TestCase):
    def test_reply_keyboard_is_one_persistent_start_button(self):
        markup = ui._persistent_reply_markup()
        self.assertTrue(markup["is_persistent"])
        self.assertFalse(markup["one_time_keyboard"])
        self.assertTrue(markup["resize_keyboard"])
        self.assertEqual(markup["keyboard"], [[{"text": "🎛 ابدأ"}]])

    def test_start_button_only_opens_menu(self):
        self.assertEqual(ui._command_kind("🎛 ابدأ"), "menu")
        buttons = [button for row in ui._main_keyboard() for button in row]
        self.assertEqual(len(buttons), 6)
        self.assertFalse(any(button.get("callback_data") == "cmd:produce_latest" for button in buttons))
        self.assertEqual(buttons[0]["callback_data"], "cmd:topic")
        self.assertEqual(buttons[2]["callback_data"], "cmd:saved")

    def test_number_shortcuts_map_to_explicit_menu_actions(self):
        expected = {
            "1": "topic",
            "١": "topic",
            "2": "short",
            "٢": "short",
            "3": "saved",
            "٣": "saved",
            "4": "used",
            "٤": "used",
            "5": "last_delivery",
            "٥": "last_delivery",
            "6": "status",
            "٦": "status",
        }
        for text, kind in expected.items():
            with self.subTest(text=text):
                self.assertEqual(ui._command_kind(text), kind)

    def test_only_exact_confirmation_phrase_gets_production_command(self):
        self.assertEqual(ui._command_kind("تأكيد الإنتاج"), "confirm_production")
        self.assertNotEqual(ui._command_kind("ابدأ الإنتاج"), "confirm_production")
        self.assertNotEqual(ui._command_kind("تأكيد الانتاج"), "confirm_production")
        self.assertNotEqual(ui._command_kind("تأكيد الإنتاج الآن"), "confirm_production")

    def test_old_inline_start_button_is_fail_closed_and_does_not_queue(self):
        request = _request()
        state = {
            "requests": {request["request_id"]: request},
            "production_queue": [],
            active.ACTIVE_RESEARCH_SESSION_KEY: "session-current",
            active.PRODUCTION_TARGET_KEY: _target(request),
        }
        client = _Client()
        with mock.patch.dict(os.environ, {"CONTROL_PLANE_PRODUCTION_ENABLED": "true"}, clear=False):
            ui._handle_command("produce_latest", client, state, None, 77)
        self.assertEqual(state["production_queue"], [])
        self.assertIn("زر بدء الإنتاج القديم", client.messages[-1][1])
        self.assertIn("تأكيد الإنتاج", client.messages[-1][1])

    def test_exact_confirmation_reuses_exact_target_queue_path(self):
        request = _request()
        state = {
            "requests": {request["request_id"]: request},
            "production_queue": [],
            "last_event_at": None,
            active.ACTIVE_RESEARCH_SESSION_KEY: "session-current",
            active.PRODUCTION_TARGET_KEY: _target(request),
        }
        client = _Client()
        with mock.patch.dict(os.environ, {"CONTROL_PLANE_PRODUCTION_ENABLED": "true"}, clear=False):
            ui._handle_command("confirm_production", client, state, None, 77)
        self.assertEqual(len(state["production_queue"]), 1)
        action = state["production_queue"][0]
        self.assertEqual(action["request_id"], request["request_id"])
        self.assertEqual(action["request_sha256"], request["request_sha256"])
        self.assertFalse(request["production_dispatch_authorized"])

    def test_confirmation_without_current_target_cannot_choose_saved_or_latest_request(self):
        old = _request("req-old")
        state = {"requests": {old["request_id"]: old}, "production_queue": [], "last_event_at": None}
        client = _Client()
        with mock.patch.dict(os.environ, {"CONTROL_PLANE_PRODUCTION_ENABLED": "true"}, clear=False):
            ui._handle_command("confirm_production", client, state, None, 77)
        self.assertEqual(state["production_queue"], [])
        self.assertIn("جلسة الاختيار الحالية", client.messages[-1][1])

    def test_new_research_action_does_not_auto_select_saved_topic(self):
        request = _request()
        state = {
            "requests": {request["request_id"]: request},
            "production_queue": [],
            "pending_actions": [],
            active.ACTIVE_RESEARCH_SESSION_KEY: "session-current",
            active.PRODUCTION_TARGET_KEY: _target(request),
            active.SAVED_SUGGESTIONS_KEY: [{"archive_id": "saved-x", "status": "available", "kind": "long", "candidate": {"title": "فكرة محفوظة"}}],
        }
        client = _Client()
        with mock.patch.object(active.simple, "_handle_command") as delegated:
            active._handle_command("topic", client, state, None, 77)
        self.assertNotIn(active.PRODUCTION_TARGET_KEY, state)
        self.assertNotIn(active.ACTIVE_RESEARCH_SESSION_KEY, state)
        self.assertEqual(state[active.SAVED_SUGGESTIONS_KEY][0]["candidate"]["title"], "فكرة محفوظة")
        delegated.assert_called_once_with("topic", client, state, None, 77)

    def test_candidate_buttons_are_numbered_and_have_detail_action(self):
        rows = ui._candidate_keyboard("session-1", "long")
        self.assertEqual(rows[0][0]["text"], "1️⃣ اختيار")
        self.assertEqual(rows[1][0]["text"], "2️⃣ اختيار")
        self.assertEqual(rows[2][0]["text"], "3️⃣ اختيار")
        self.assertEqual(rows[0][1]["text"], "🔎 تفاصيل")
        self.assertEqual(rows[0][0]["callback_data"], "pick:session-1:0")

    def test_approval_copy_requires_text_confirmation_and_has_no_start_button(self):
        request = _request()
        with mock.patch.dict(os.environ, {"CONTROL_PLANE_PRODUCTION_ENABLED": "true"}, clear=False):
            text = ui._approval_text(request)
        self.assertIn("لم يبدأ الإنتاج بعد", text)
        self.assertIn("تأكيد الإنتاج", text)
        self.assertIn("أي زر تشغيل قديم", text)
        self.assertFalse(any(button.get("callback_data") == "cmd:produce_latest" for row in ui._main_keyboard() for button in row))

    def test_persistent_surface_installs_once_and_requires_all_identity_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            panel.save_state(
                state_path,
                {"schema_version": 1, "telegram_offset": 0, "sessions": {}, "requests": {}, "pending_actions": [], "production_queue": [], "last_event_at": None},
            )
            client = _Client()
            secret_map = {
                "TELEGRAM_BOT_TOKEN_FILE": "token",
                "TELEGRAM_CHAT_ID_FILE": "77",
                "TELEGRAM_ALLOWED_USER_ID_FILE": "88",
            }
            with mock.patch.object(panel, "_read_secret_file", side_effect=lambda key, required=False: secret_map.get(key)), \
                 mock.patch.object(panel, "TelegramClient", return_value=client):
                ui._ensure_persistent_start_surface(state_path)
                ui._ensure_persistent_start_surface(state_path)
            self.assertEqual(len(client.calls), 1)
            method, payload = client.calls[0]
            self.assertEqual(method, "sendMessage")
            self.assertEqual(payload["reply_markup"]["keyboard"], [[{"text": "🎛 ابدأ"}]])
            saved = panel.load_state(state_path)
            self.assertEqual(saved[ui.PERSISTENT_SURFACE_STATE_KEY], ui.PERSISTENT_SURFACE_VERSION)

    def test_surface_install_is_noop_when_allowed_user_secret_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            panel.save_state(
                state_path,
                {"schema_version": 1, "telegram_offset": 0, "sessions": {}, "requests": {}, "pending_actions": [], "production_queue": [], "last_event_at": None},
            )
            secret_map = {
                "TELEGRAM_BOT_TOKEN_FILE": "token",
                "TELEGRAM_CHAT_ID_FILE": "77",
                "TELEGRAM_ALLOWED_USER_ID_FILE": "",
            }
            with mock.patch.object(panel, "_read_secret_file", side_effect=lambda key, required=False: secret_map.get(key)), \
                 mock.patch.object(panel, "TelegramClient") as client_cls:
                ui._ensure_persistent_start_surface(state_path)
            client_cls.assert_not_called()
            saved = panel.load_state(state_path)
            self.assertNotIn(ui.PERSISTENT_SURFACE_STATE_KEY, saved)


if __name__ == "__main__":
    unittest.main()

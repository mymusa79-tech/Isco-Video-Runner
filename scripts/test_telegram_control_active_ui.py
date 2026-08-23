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


def _candidate(title: str, *, score: float = 0.8) -> dict:
    return {
        "title": title,
        "control_score": score,
        "opportunity_score": score,
        "audience_fit": score,
        "hook_potential": score,
        "retention_potential": score,
        "title_thumbnail_potential": score,
        "evergreen_score": score,
        "trend_score": score,
        "production_feasibility": score,
        "evidence_quality": score,
        "evidence": ["دليل محفوظ"],
        "why": ["ملاءمة قوية لجمهور القناة"],
        "approved_research_pack": [
            {"source_title": "Source A", "source_url": "https://example.com/a", "claim_scope": "scope a"},
            {"source_title": "Source B", "source_url": "https://example.com/b", "claim_scope": "scope b"},
        ],
    }


class TelegramActiveUiTests(unittest.TestCase):
    def test_enabled_surface_has_one_explicit_start_button(self):
        with mock.patch.dict(os.environ, {"CONTROL_PLANE_PRODUCTION_ENABLED": "true"}, clear=False):
            buttons = [button for row in ui._main_keyboard() for button in row]
            starts = [button for button in buttons if button.get("callback_data") == "cmd:produce_latest"]
            self.assertEqual(len(starts), 1)
            self.assertIn("🚀", starts[0]["text"])
            self.assertEqual(len([button for button in buttons if button.get("callback_data") == "cmd:saved"]), 1)
            menu = ui._menu_text()
            self.assertIn("إنتاج Telegram مفعّل", menu)
            self.assertIn("ضغطة مستقلة", menu)
            self.assertIn("لا نشر إلى YouTube", menu)
            self.assertIn("الاقتراحات", menu)

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
        self.assertIn("جلسة الاختيار الحالية", client.messages[-1][1])

    def test_start_button_queues_exact_current_selection_not_latest_saved_request(self):
        selected = _request("req-selected", approved_at="2026-08-23T07:00:00+00:00")
        newer_saved = _request("req-newer-saved", approved_at="2026-08-23T08:00:00+00:00")
        state = {
            "requests": {selected["request_id"]: selected, newer_saved["request_id"]: newer_saved},
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
        state = {"requests": {}, "production_queue": [], ui.ACTIVE_RESEARCH_SESSION_KEY: "session-current"}
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

    def test_research_candidates_are_saved_as_non_executable_ideas_and_deduplicated(self):
        state = {"sessions": {}, "requests": {}, "pending_actions": []}
        first = {
            "session_id": "s1",
            "kind": "long",
            "candidates": [_candidate("أ"), _candidate("ب"), _candidate("ج")],
        }
        ids = ui._archive_session_candidates(state, first)
        self.assertEqual(len(ids), 3)
        self.assertEqual(len(ui._available_saved(state)), 3)
        for item in ui._available_saved(state):
            self.assertNotIn("request_id", item)
            self.assertNotIn("production_dispatch_authorized", item)
        second = {"session_id": "s2", "kind": "long", "candidates": [_candidate("  أ  ", score=0.9)]}
        second_ids = ui._archive_session_candidates(state, second)
        self.assertEqual(second_ids[0], ids[0])
        self.assertEqual(len(ui._available_saved(state)), 3)

    def test_saved_archive_is_bounded(self):
        state = {"sessions": {}, "requests": {}, "pending_actions": []}
        session = {
            "session_id": "bulk",
            "kind": "short",
            "candidates": [_candidate(f"موضوع {index}") for index in range(35)],
        }
        ui._archive_session_candidates(state, session)
        self.assertEqual(len(ui._available_saved(state)), ui.MAX_SAVED_SUGGESTIONS)

    def test_opening_saved_idea_creates_fresh_session_without_approval_or_queue(self):
        state = {"sessions": {}, "requests": {}, "pending_actions": [], "production_queue": []}
        source = {"session_id": "s1", "kind": "long", "candidates": [_candidate("موضوع محفوظ")]}
        archive_id = ui._archive_session_candidates(state, source)[0]
        session = ui._activate_saved_suggestion(state, archive_id)
        self.assertEqual(session["source"], "saved_suggestion")
        self.assertEqual(state[ui.ACTIVE_RESEARCH_SESSION_KEY], session["session_id"])
        self.assertNotIn(ui.PRODUCTION_TARGET_KEY, state)
        self.assertEqual(state["requests"], {})
        self.assertEqual(state["production_queue"], [])

    def test_approving_saved_idea_removes_only_it_and_binds_exact_target(self):
        state = {"sessions": {}, "requests": {}, "pending_actions": [], "production_queue": []}
        source = {
            "session_id": "s1",
            "kind": "long",
            "candidates": [_candidate("اخترني"), _candidate("ابقني")],
        }
        ids = ui._archive_session_candidates(state, source)
        session = ui._activate_saved_suggestion(state, ids[0])
        request = _request("req-saved")
        state["requests"][request["request_id"]] = request
        with mock.patch.object(ui.simple, "_approve", return_value=request):
            ui._approve_current(state, session, 0, "long")
        remaining = [item["candidate"]["title"] for item in ui._available_saved(state)]
        self.assertEqual(remaining, ["ابقني"])
        self.assertEqual(state[ui.PRODUCTION_TARGET_KEY]["request_id"], "req-saved")

    def test_saved_pick_waits_for_running_research(self):
        state = {
            "sessions": {},
            "requests": {},
            "production_queue": [],
            "pending_actions": [{"kind": "long", "status": "pending"}],
        }
        source = {"session_id": "s1", "kind": "long", "candidates": [_candidate("موضوع محفوظ")]}
        archive_id = ui._archive_session_candidates(state, source)[0]
        with self.assertRaisesRegex(RuntimeError, "research to finish"):
            ui._activate_saved_suggestion(state, archive_id)
        self.assertNotIn(ui.ACTIVE_RESEARCH_SESSION_KEY, state)

    def test_saved_page_is_paginated_and_has_no_direct_start_button(self):
        state = {"sessions": {}, "requests": {}, "pending_actions": []}
        session = {
            "session_id": "s1",
            "kind": "short",
            "candidates": [_candidate(f"فكرة {index}") for index in range(7)],
        }
        ui._archive_session_candidates(state, session)
        text, keyboard = ui._saved_page(state, 0)
        buttons = [button for row in keyboard for button in row]
        self.assertIn("صفحة 1/2", text)
        self.assertEqual(sum(str(button.get("callback_data", "")).startswith("cmd:savedpick-") for button in buttons), 5)
        self.assertFalse(any(button.get("callback_data") == "cmd:produce_latest" for button in buttons))
        self.assertTrue(any(button.get("callback_data") == "cmd:savedpage-1" for button in buttons))

    def test_saved_command_aliases(self):
        self.assertEqual(ui._command_kind("الاقتراحات المحفوظة"), "saved")
        self.assertEqual(ui._command_kind("المحفوظة"), "saved")


if __name__ == "__main__":
    unittest.main()

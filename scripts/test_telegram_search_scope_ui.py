from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import telegram_control_active_ui as active
from scripts import telegram_control_panel as panel
from scripts import telegram_search_scope_ui as scope_ui
from scripts import telegram_topic_memory_ui as memory_ui


class _Client:
    def __init__(self, token: str = "token"):
        self.token = token
        self.messages = []
        self.answers = []

    def call(self, method, payload=None):
        if method == "getUpdates":
            return [
                {
                    "update_id": 101,
                    "callback_query": {
                        "id": "cb-1",
                        "from": {"id": 88},
                        "message": {"chat": {"id": 77}},
                        "data": "pick:session-current:0",
                    },
                }
            ]
        raise AssertionError(f"unexpected method: {method}")

    def answer_callback(self, callback_id, text=""):
        self.answers.append((callback_id, text))

    def send(self, chat_id, text, *, keyboard=None):
        self.messages.append((chat_id, text, keyboard))


class TelegramSearchScopeUiTests(unittest.TestCase):
    def test_search_menu_exposes_exact_three_modes_without_production_callback(self):
        rows = memory_ui._scoped_search_keyboard()
        callbacks = [button.get("callback_data") for row in rows for button in row if button.get("callback_data")]
        self.assertEqual(
            callbacks,
            ["cmd:topic_bundle", "cmd:topic_long", "cmd:short", "cmd:menu"],
        )
        labels = [row[0]["text"] for row in rows[:3]]
        self.assertEqual(labels, ["🎬➕⚡ حلقة + Shorts", "🎬 حلقة فقط", "⚡ Short فقط"])
        self.assertFalse(any("produce" in value for value in callbacks))

    def test_scope_binds_only_when_a_new_long_request_was_actually_queued(self):
        state = {"pending_actions": []}
        before = scope_ui.pending_long_ids(state)
        state["pending_actions"].append(
            {"action_id": "new-long", "kind": "long", "status": "pending"}
        )
        self.assertTrue(scope_ui.bind_scope_to_new_long_request(state, scope="bundle", before_ids=before))
        self.assertEqual(state[scope_ui.SEARCH_SCOPE_HINT_KEY]["scope"], "bundle")

        before = scope_ui.pending_long_ids(state)
        self.assertFalse(scope_ui.bind_scope_to_new_long_request(state, scope="long", before_ids=before))
        # A duplicate click must not silently change the already queued request's mode.
        self.assertEqual(state[scope_ui.SEARCH_SCOPE_HINT_KEY]["scope"], "bundle")

    def test_scoped_command_translates_to_existing_long_research_and_binds_hint(self):
        state = {"pending_actions": []}
        client = _Client()
        observed = []

        def base_handler(kind, _client, target_state, _releases, _chat_id):
            observed.append(kind)
            target_state["pending_actions"].append(
                {"action_id": "queued-long", "kind": "long", "status": "pending"}
            )

        with mock.patch.object(active, "_ISCO_RESEARCH_CLARITY_BASE_HANDLE", base_handler, create=True):
            memory_ui._handle_command_with_research_clarity("topic_bundle", client, state, None, 77)

        self.assertEqual(observed, ["topic"])
        self.assertEqual(state[scope_ui.SEARCH_SCOPE_HINT_KEY]["scope"], "bundle")
        self.assertTrue(any("حلقة + Shorts" in text for _, text, _ in client.messages))

    def test_saved_session_never_inherits_search_scope_hint(self):
        state = {
            scope_ui.SEARCH_SCOPE_HINT_KEY: {"kind": "long", "scope": "bundle"},
            scope_ui.ACTIVE_RESEARCH_SESSION_KEY: "saved-session",
        }
        saved = {"session_id": "saved-session", "kind": "long", "source": "saved_suggestion"}
        self.assertIsNone(scope_ui.preferred_scope_for_session(state, saved))

    def test_preselected_bundle_scope_is_consumed_on_candidate_pick(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            panel.save_state(
                state_path,
                {
                    "schema_version": 1,
                    "telegram_offset": 0,
                    "sessions": {
                        "session-current": {
                            "session_id": "session-current",
                            "kind": "long",
                            "candidates": [{"title": "الفكرة الأولى"}],
                        }
                    },
                    "requests": {},
                    "pending_actions": [],
                    scope_ui.ACTIVE_RESEARCH_SESSION_KEY: "session-current",
                    scope_ui.SEARCH_SCOPE_HINT_KEY: {"kind": "long", "scope": "bundle"},
                    "last_event_at": None,
                },
            )
            client = _Client()
            secret_map = {
                "TELEGRAM_BOT_TOKEN_FILE": "token",
                "TELEGRAM_ALLOWED_USER_ID_FILE": "88",
                "TELEGRAM_CHAT_ID_FILE": "77",
            }
            approved = {
                "approved_topic": "الفكرة الأولى",
                "approval_scope": "long_plus_sibling_shorts",
                "request_id": "req-test",
            }
            with mock.patch.object(panel, "_read_secret_file", side_effect=lambda name, required=False: secret_map.get(name, "")), \
                 mock.patch.object(panel, "TelegramClient", return_value=client), \
                 mock.patch.object(panel, "GitHubReleaseClient"), \
                 mock.patch.object(active, "_approve_current", return_value=approved) as approve, \
                 mock.patch.object(active, "_approval_text", return_value="APPROVED"), \
                 mock.patch.object(active, "_main_keyboard", return_value=[]):
                scope_ui.poll(state_path)

            approve.assert_called_once()
            self.assertEqual(approve.call_args.args[3], "bundle")
            self.assertTrue(any(text == "APPROVED" for _, text, _ in client.messages))
            self.assertFalse(any("اختر نطاق الإنتاج" in text for _, text, _ in client.messages))
            saved = panel.load_state(state_path)
            self.assertNotIn(scope_ui.SEARCH_SCOPE_HINT_KEY, saved)


if __name__ == "__main__":
    unittest.main()

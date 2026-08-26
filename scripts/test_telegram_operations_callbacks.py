from __future__ import annotations

import copy
import unittest

from scripts import telegram_control_simple_ui as simple


class FakeClient:
    def __init__(self) -> None:
        self.calls = []
        self.sent = []

    def call(self, method, payload=None):
        self.calls.append((method, payload or {}))
        return True

    def send(self, chat_id, text, *, keyboard=None):
        self.sent.append((chat_id, text, keyboard))
        return True


class FakeReleases:
    repository = "mymusa79-tech/Isco-Video-Runner"

    def __init__(self, *, returned_run_id: int = 123) -> None:
        self.returned_run_id = returned_run_id

    def _get(self, url: str):
        if url.endswith("/jobs"):
            return {
                "jobs": [
                    {
                        "name": "produce",
                        "conclusion": "failure",
                        "steps": [
                            {"name": "Checkout", "conclusion": "success"},
                            {"name": "Final review", "conclusion": "failure"},
                        ],
                    }
                ]
            }
        return {
            "id": self.returned_run_id,
            "run_number": 112,
            "name": "Isco Video Production Resilient V4",
            "status": "completed",
            "conclusion": "failure",
            "head_branch": "main",
            "event": "workflow_dispatch",
            "html_url": f"https://github.com/mymusa79-tech/Isco-Video-Runner/actions/runs/{self.returned_run_id}",
            "jobs_url": f"https://api.github.com/repos/mymusa79-tech/Isco-Video-Runner/actions/runs/{self.returned_run_id}/jobs",
            "repository": {"full_name": self.repository},
        }

    def latest(self, prefix=None):
        return None


class OperationsToggleTests(unittest.TestCase):
    def test_details_edits_same_message_and_does_not_mutate_state(self) -> None:
        client = FakeClient()
        releases = FakeReleases(returned_run_id=123)
        state = {"requests": {"r": {"status": "approved"}}, "pending_actions": []}
        before = copy.deepcopy(state)
        simple._handle_command("opsdetails-123-42", client, state, releases, "chat")
        self.assertEqual(state, before)
        self.assertEqual(len(client.calls), 1)
        method, payload = client.calls[0]
        self.assertEqual(method, "editMessageText")
        self.assertEqual(payload["message_id"], 42)
        self.assertIn("📋 تفاصيل التشغيل", payload["text"])
        self.assertIn("Final review", payload["text"])
        buttons = [button for row in payload["reply_markup"]["inline_keyboard"] for button in row]
        self.assertTrue(any(button.get("callback_data") == "cmd:opscompact-123-42" for button in buttons))

    def test_compact_restores_same_message_and_is_idempotent(self) -> None:
        client = FakeClient()
        releases = FakeReleases(returned_run_id=123)
        state = {"requests": {}, "pending_actions": []}
        before = copy.deepcopy(state)
        simple._handle_command("opscompact-123-42", client, state, releases, "chat")
        simple._handle_command("opscompact-123-42", client, state, releases, "chat")
        self.assertEqual(state, before)
        self.assertEqual(len(client.calls), 2)
        for method, payload in client.calls:
            self.assertEqual(method, "editMessageText")
            self.assertEqual(payload["message_id"], 42)
            self.assertIn("❌ فشل الإنتاج", payload["text"])

    def test_malformed_operations_callback_has_no_side_effect(self) -> None:
        client = FakeClient()
        releases = FakeReleases(returned_run_id=123)
        state = {"requests": {"r": 1}, "pending_actions": [1]}
        before = copy.deepcopy(state)
        simple._handle_command("opsdetails-not-valid", client, state, releases, "chat")
        self.assertEqual(state, before)
        self.assertEqual(client.calls, [])
        self.assertEqual(client.sent, [])

    def test_wrong_run_id_binding_has_no_side_effect(self) -> None:
        client = FakeClient()
        releases = FakeReleases(returned_run_id=999)
        state = {"requests": {"r": 1}, "pending_actions": []}
        before = copy.deepcopy(state)
        simple._handle_command("opsdetails-123-42", client, state, releases, "chat")
        self.assertEqual(state, before)
        self.assertEqual(client.calls, [])
        self.assertEqual(client.sent, [])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.channel_os_memory import LiveState
from scripts.channel_os_telegram_adapter import (
    callback_view,
    render_control_state,
    video_entities_from_control_state,
)


class Provider:
    def __init__(self, mapping=None, failures=None):
        self.mapping = mapping or {}
        self.failures = set(failures or ())
        self.calls = []

    def fetch(self, video_id):
        self.calls.append(video_id)
        if video_id in self.failures:
            raise RuntimeError("live source unavailable")
        return self.mapping[video_id]


def live(video_id: str, status: str, run_id: str, reason: str = "") -> LiveState:
    return LiveState(
        video_id=video_id,
        status=status,
        stage="production",
        run_id=run_id,
        source="github-actions",
        observed_at="2026-08-30T00:00:00+00:00",
        reason=reason,
    )


class ChannelOSTelegramAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _state(self):
        return {
            "schema_version": 1,
            "sessions": {},
            "pending_actions": [],
            "saved_suggestions": [
                {
                    "archive_id": "idea-1",
                    "status": "available",
                    "candidate": {"title": "فكرة محفوظة"},
                }
            ],
            "requests": {
                "req-1": {
                    "request_id": "req-1",
                    "approved_topic": "موضوع معتمد",
                    "approved_by_user": True,
                    "approved_at": "2026-08-29T20:00:00+00:00",
                    "status": "approved_waiting_production_activation",
                }
            },
            "production_queue": [
                {
                    "request_id": "req-1",
                    "attempt": 1,
                    "status": "dispatch_consumed",
                    "consumed_at": "2026-08-29T20:05:00+00:00",
                    "workflow_run_id": "12345",
                }
            ],
        }

    def test_callback_surface_is_exactly_three_read_views(self):
        self.assertEqual(callback_view("cmd:channelos-refresh"), "all")
        self.assertEqual(callback_view("cmd:channelos-needs"), "needs")
        self.assertEqual(callback_view("cmd:channelos-problems"), "problems")
        self.assertIsNone(callback_view("cmd:channelos-produce"))
        self.assertIsNone(callback_view("approve:anything"))

    def test_control_state_projects_saved_as_ideas_and_approved_as_ready(self):
        entities = video_entities_from_control_state(self._state())
        by_id = {item.video_id: item for item in entities}
        self.assertEqual(by_id["saved:idea-1"].durable_state, "Ideas")
        self.assertEqual(by_id["req-1"].durable_state, "Ready")
        self.assertEqual(by_id["req-1"].run_id, "12345")

    def test_successful_production_never_implies_published(self):
        provider = Provider({"req-1": live("req-1", "success", "12345")})
        text, _ = render_control_state(
            self._state(),
            repository="owner/repo",
            github_token="",
            memory_root=self.tmp.name,
            live_provider=provider,
        )
        self.assertIn("Ready: 1", text)
        self.assertIn("Published: 0", text)
        self.assertIn("لا ينشر إلى YouTube", text)

    def test_live_failure_beats_ready_metadata_and_appears_in_problems(self):
        provider = Provider({"req-1": live("req-1", "failed", "12345", "provider exhausted")})
        text, _ = render_control_state(
            self._state(),
            repository="owner/repo",
            github_token="",
            memory_root=self.tmp.name,
            view="problems",
            live_provider=provider,
        )
        self.assertIn("Problems: 1", text)
        self.assertIn("provider exhausted", text)
        self.assertNotIn("موضوع معتمد\n", text)

    def test_live_source_unavailable_is_problem_not_cached_success(self):
        provider = Provider(failures={"req-1"})
        text, _ = render_control_state(
            self._state(),
            repository="owner/repo",
            github_token="",
            memory_root=self.tmp.name,
            view="problems",
            live_provider=provider,
        )
        self.assertIn("Problems: 1", text)
        self.assertIn("Live State unavailable", text)

    def test_needs_view_only_returns_action_required_items(self):
        provider = Provider({"req-1": live("req-1", "action_required", "12345", "editorial choice")})
        text, keyboard = render_control_state(
            self._state(),
            repository="owner/repo",
            github_token="",
            memory_root=self.tmp.name,
            view="needs",
            live_provider=provider,
        )
        self.assertIn("Needs Me: 1", text)
        self.assertIn("editorial choice", text)
        self.assertTrue(any(button.get("callback_data") == "cmd:menu" for row in keyboard for button in row))

    def test_adapter_has_no_telegram_transport_or_secret_ownership(self):
        source = Path("scripts/channel_os_telegram_adapter.py").read_text(encoding="utf-8")
        forbidden = (
            "TELEGRAM_BOT_TOKEN",
            "getUpdates",
            "sendMessage",
            "api.telegram.org",
            "TelegramClient",
            "requests.post",
        )
        for token in forbidden:
            self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main()

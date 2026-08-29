from __future__ import annotations

import copy
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import channel_os_telegram_adapter as adapter
from scripts import telegram_control_active_ui as active
from scripts.channel_os_memory import LiveState


class Client:
    def __init__(self) -> None:
        self.sent = []

    def send(self, chat_id, text, keyboard=None):
        self.sent.append((chat_id, text, keyboard))


class Releases:
    repository = "mymusa79-tech/Isco-Video-Runner"


def approved(request_id: str, topic: str) -> dict:
    return {
        "request_id": request_id,
        "approved_topic": topic,
        "approved_by_user": True,
        "status": "approved_waiting_production_activation",
    }


class ChannelOSTelegramAdapterTests(unittest.TestCase):
    def test_entities_use_only_approved_requests(self) -> None:
        state = {
            "requests": {
                "r1": approved("r1", "موضوع أول"),
                "r2": {"request_id": "r2", "approved_topic": "غير معتمد", "approved_by_user": False},
                "broken": "not-an-object",
            }
        }
        entities = adapter.entities_from_control_state(state)
        self.assertEqual([(item.video_id, item.title) for item in entities], [("r1", "موضوع أول")])

    def test_latest_queue_attempt_owns_run_binding_and_old_run_is_not_reused(self) -> None:
        state = {
            "requests": {"r1": approved("r1", "موضوع")},
            "production_queue": [
                {"request_id": "r1", "attempt": 1, "requested_at": "2026-08-29T10:00:00+00:00", "workflow_run_id": "111"},
                {"request_id": "r1", "attempt": 2, "requested_at": "2026-08-29T11:00:00+00:00", "status": "pending_dispatch"},
            ],
        }
        self.assertEqual(adapter.entities_from_control_state(state)[0].run_id, "")
        state["production_queue"][-1]["workflow_run_id"] = "222"
        self.assertEqual(adapter.entities_from_control_state(state)[0].run_id, "222")

    def test_production_completion_never_fabricates_youtube_publication(self) -> None:
        request = approved("r1", "موضوع")
        request["status"] = "completed"
        state = {
            "requests": {"r1": request},
            "used_topics": [{"request_id": "r1", "topic": "موضوع", "release_tag": "video-r1"}],
        }
        entity = adapter.entities_from_control_state(state)[0]
        self.assertEqual(entity.durable_state, "Ready")
        self.assertNotEqual(entity.durable_state, "Published")
        self.assertNotEqual(entity.durable_state, "Scheduled")

    def test_live_refresh_reads_exact_run_without_cached_fallback(self) -> None:
        state = {
            "requests": {"r1": approved("r1", "موضوع")},
            "production_queue": [{"request_id": "r1", "attempt": 1, "workflow_run_id": "77"}],
        }
        calls = []

        def fetch(provider, video_id):
            calls.append((video_id, provider.run_bindings[video_id]))
            return LiveState(
                video_id=video_id,
                status="running",
                stage="production",
                run_id="77",
                source="github-actions",
                observed_at="2026-08-29T20:00:00+00:00",
            )

        with patch.object(adapter.GitHubActionsLiveStateProvider, "fetch", fetch):
            text, _ = adapter.render_from_control_state(
                state, repository="mymusa79-tech/Isco-Video-Runner", token="token"
            )
            adapter.render_from_control_state(
                state, repository="mymusa79-tech/Isco-Video-Runner", token="token"
            )
        self.assertEqual(calls, [("r1", "77"), ("r1", "77")])
        self.assertIn("Producing", text)

    def test_live_source_failure_projects_problem_without_stale_substitution(self) -> None:
        state = {
            "requests": {"r1": approved("r1", "موضوع")},
            "production_queue": [{"request_id": "r1", "attempt": 1, "workflow_run_id": "77"}],
        }
        with patch.object(
            adapter.GitHubActionsLiveStateProvider,
            "fetch",
            side_effect=RuntimeError("live source unavailable"),
        ):
            text, _ = adapter.render_from_control_state(
                state,
                repository="mymusa79-tech/Isco-Video-Runner",
                mission_state="Problems",
            )
        self.assertIn("Problems", text)
        self.assertIn("Live State unavailable", text)
        self.assertIn("live source unavailable", text)

    def test_missing_github_repository_fails_closed_for_run_bound_item(self) -> None:
        state = {
            "requests": {"r1": approved("r1", "موضوع")},
            "production_queue": [{"request_id": "r1", "attempt": 1, "workflow_run_id": "77"}],
        }
        text, _ = adapter.render_from_control_state(state, repository="", mission_state="Problems")
        self.assertIn("Problems", text)
        self.assertIn("GitHub repository identity is unavailable", text)

    def test_filter_commands_are_read_only_and_unknown_command_delegates(self) -> None:
        state = {"requests": {"r1": approved("r1", "موضوع")}}
        original = copy.deepcopy(state)
        client = Client()
        delegated = []
        sentinel = getattr(active, "_ISCO_CHANNEL_OS_BASE_HANDLE", None)
        active._ISCO_CHANNEL_OS_BASE_HANDLE = lambda *args: delegated.append(args[0])
        try:
            with patch.dict(os.environ, {"GITHUB_TOKEN": "token"}, clear=False):
                adapter._handle_command_with_channel_os(
                    "channelos-refresh", client, state, Releases(), 123
                )
                adapter._handle_command_with_channel_os(
                    "something-else", client, state, Releases(), 123
                )
        finally:
            if sentinel is None:
                delattr(active, "_ISCO_CHANNEL_OS_BASE_HANDLE")
            else:
                active._ISCO_CHANNEL_OS_BASE_HANDLE = sentinel
        self.assertEqual(state, original)
        self.assertEqual(len(client.sent), 1)
        self.assertEqual(delegated, ["something-else"])

    def test_needs_and_problems_filters_do_not_mutate_counts_or_state(self) -> None:
        state = {
            "requests": {
                "r1": approved("r1", "يحتاجني"),
                "r2": approved("r2", "مشكلة"),
            },
            "production_queue": [
                {"request_id": "r1", "attempt": 1, "workflow_run_id": "11"},
                {"request_id": "r2", "attempt": 1, "workflow_run_id": "22"},
            ],
        }
        original = copy.deepcopy(state)

        def fetch(provider, video_id):
            status = "action_required" if video_id == "r1" else "failed"
            run_id = provider.run_bindings[video_id]
            return LiveState(
                video_id, status, "production", run_id, "github-actions",
                "2026-08-29T20:00:00+00:00", True, "gate" if status == "failed" else "approval"
            )

        with patch.object(adapter.GitHubActionsLiveStateProvider, "fetch", fetch):
            needs, _ = adapter.render_from_control_state(
                state, repository=Releases.repository, mission_state="Needs Me"
            )
            problems, _ = adapter.render_from_control_state(
                state, repository=Releases.repository, mission_state="Problems"
            )
        self.assertIn("يحتاجني", needs)
        self.assertNotIn("مشكلة · Run", needs)
        self.assertIn("مشكلة", problems)
        self.assertNotIn("يحتاجني · Run", problems)
        self.assertEqual(state, original)

    def test_adapter_source_has_no_telegram_or_production_transport_authority(self) -> None:
        source = Path("scripts/channel_os_telegram_adapter.py").read_text(encoding="utf-8")
        for forbidden in (
            "getUpdates",
            "TELEGRAM_BOT_TOKEN",
            "telegram_outbox_runtime",
            "enqueue_request",
            "sendMessage",
            "gh workflow run",
        ):
            self.assertNotIn(forbidden, source)

    def test_install_adds_one_channel_os_entry_and_is_idempotent(self) -> None:
        original_installed = adapter._INSTALLED
        original_handler = active._handle_command
        original_keyboard = active._main_keyboard
        had_base_handler = hasattr(active, "_ISCO_CHANNEL_OS_BASE_HANDLE")
        base_handler = getattr(active, "_ISCO_CHANNEL_OS_BASE_HANDLE", None)
        had_base_keyboard = hasattr(active, "_ISCO_CHANNEL_OS_BASE_KEYBOARD")
        base_keyboard = getattr(active, "_ISCO_CHANNEL_OS_BASE_KEYBOARD", None)
        try:
            adapter._INSTALLED = False
            if had_base_handler:
                delattr(active, "_ISCO_CHANNEL_OS_BASE_HANDLE")
            if had_base_keyboard:
                delattr(active, "_ISCO_CHANNEL_OS_BASE_KEYBOARD")
            adapter.install()
            first_handler = active._handle_command
            first_keyboard = active._main_keyboard
            adapter.install()
            self.assertIs(active._handle_command, first_handler)
            self.assertIs(active._main_keyboard, first_keyboard)
            rows = active._main_keyboard()
            callbacks = [
                button.get("callback_data")
                for row in rows
                for button in row
                if isinstance(button, dict)
            ]
            self.assertEqual(callbacks.count("cmd:channelos-refresh"), 1)
        finally:
            adapter._INSTALLED = original_installed
            active._handle_command = original_handler
            active._main_keyboard = original_keyboard
            if had_base_handler:
                active._ISCO_CHANNEL_OS_BASE_HANDLE = base_handler
            elif hasattr(active, "_ISCO_CHANNEL_OS_BASE_HANDLE"):
                delattr(active, "_ISCO_CHANNEL_OS_BASE_HANDLE")
            if had_base_keyboard:
                active._ISCO_CHANNEL_OS_BASE_KEYBOARD = base_keyboard
            elif hasattr(active, "_ISCO_CHANNEL_OS_BASE_KEYBOARD"):
                delattr(active, "_ISCO_CHANNEL_OS_BASE_KEYBOARD")


if __name__ == "__main__":
    unittest.main()

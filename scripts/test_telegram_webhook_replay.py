from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import telegram_control_active_ui as active
from scripts import telegram_control_panel as panel
from scripts import telegram_webhook_replay as replay
from scripts.orchestration_telegram_ingress_outbox import (
    ApprovalDecision,
    ReleaseCandidateDigest,
    TelegramControlContractError,
)
from scripts.telegram_release_approval import callback_data_for


def _state(path: Path) -> None:
    panel.save_state(
        path,
        {
            "schema_version": 1,
            "telegram_offset": 0,
            "sessions": {},
            "requests": {},
            "pending_actions": [],
            "production_queue": [],
            "last_event_at": None,
        },
    )


def _update(update_id: int = 101) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": "cb-1",
            "from": {"id": 88},
            "message": {"message_id": 42, "chat": {"id": 77}},
            "data": "cmd:menu",
        },
    }


def _candidate() -> ReleaseCandidateDigest:
    return ReleaseCandidateDigest(
        run_id="run-1",
        final_mp4_sha256="a" * 64,
        delivery_manifest_sha256="b" * 64,
        capability_manifest_sha256="c" * 64,
        release_asset_set_digest="d" * 64,
    )


def _release_update(update_id: int = 201, *, user_id: int = 88, chat_id: int = 77) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"approval-{update_id}",
            "from": {"id": user_id},
            "message": {"message_id": 99, "chat": {"id": chat_id}},
            "data": callback_data_for(_candidate(), ApprovalDecision.APPROVED),
        },
    }


def _secret(name: str, *, required: bool = False) -> str:
    values = {
        "TELEGRAM_ALLOWED_USER_ID_FILE": "88",
        "TELEGRAM_CHAT_ID_FILE": "77",
    }
    return values.get(name, "")


class TelegramWebhookReplayTests(unittest.TestCase):
    def test_decode_update_accepts_bounded_callback_json(self):
        encoded = base64.b64encode(json.dumps(_update()).encode("utf-8")).decode("ascii")
        self.assertEqual(replay.decode_update(encoded)["update_id"], 101)

    def test_decode_update_rejects_malformed_or_unsupported_payload(self):
        with self.assertRaises(RuntimeError):
            replay.decode_update("not-base64")
        encoded = base64.b64encode(json.dumps({"update_id": 1}).encode("utf-8")).decode("ascii")
        with self.assertRaises(RuntimeError):
            replay.decode_update(encoded)

    def test_replay_injects_one_update_and_suppresses_second_callback_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            _state(path)
            observed = {"updates": None, "answered": False, "runs": 0}

            def fake_poll(state_path: Path) -> None:
                observed["runs"] += 1
                client = panel.TelegramClient("token")
                observed["updates"] = client.call("getUpdates", {})
                client.answer_callback("cb-1")
                state = panel.load_state(state_path)
                state["last_event_at"] = panel._now()
                panel.save_state(state_path, state)

            def original_call(self, method, payload=None):
                raise AssertionError(f"unexpected network call: {method}")

            with mock.patch.object(replay.memory_ui, "_install_policy"), \
                 mock.patch("scripts.telegram_persistent_control_ui.install"), \
                 mock.patch.object(active, "_poll", side_effect=fake_poll), \
                 mock.patch.object(panel.TelegramClient, "call", new=original_call):
                self.assertTrue(replay.replay_update(path, _update()))

            self.assertEqual(observed["runs"], 1)
            self.assertEqual(observed["updates"], [_update()])
            saved = panel.load_state(path)
            self.assertEqual(saved[replay.SEEN_UPDATES_KEY], [101])

    def test_duplicate_update_has_no_second_side_effect(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            _state(path)
            state = panel.load_state(path)
            state[replay.SEEN_UPDATES_KEY] = [101]
            panel.save_state(path, state)
            with mock.patch.object(active, "_poll") as poll:
                self.assertFalse(replay.replay_update(path, _update()))
            poll.assert_not_called()

    def test_out_of_order_unique_updates_are_not_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            _state(path)
            state = panel.load_state(path)
            state[replay.SEEN_UPDATES_KEY] = [105]
            panel.save_state(path, state)

            def fake_poll(state_path: Path) -> None:
                state = panel.load_state(state_path)
                panel.save_state(state_path, state)

            with mock.patch.object(replay.memory_ui, "_install_policy"), \
                 mock.patch("scripts.telegram_persistent_control_ui.install"), \
                 mock.patch.object(active, "_poll", side_effect=fake_poll):
                self.assertTrue(replay.replay_update(path, _update(104)))
            saved = panel.load_state(path)
            self.assertEqual(saved[replay.SEEN_UPDATES_KEY], [105, 104])

    def test_release_approval_only_consumes_approval_without_legacy_poll(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            _state(path)
            with mock.patch.object(panel, "_read_secret_file", side_effect=_secret), \
                 mock.patch.object(active, "_poll") as poll:
                self.assertTrue(replay.replay_release_approval_only(path, _release_update()))
            poll.assert_not_called()
            state = panel.load_state(path)
            self.assertEqual(state[replay.SEEN_UPDATES_KEY], [201])
            self.assertEqual(len(state["release_approval_receipts"]), 1)

    def test_release_approval_only_rejects_non_release_command_without_marking_seen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            _state(path)
            with mock.patch.object(panel, "_read_secret_file", side_effect=_secret), \
                 mock.patch.object(active, "_poll") as poll:
                self.assertFalse(replay.replay_release_approval_only(path, _update(202)))
            poll.assert_not_called()
            state = panel.load_state(path)
            self.assertNotIn(replay.SEEN_UPDATES_KEY, state)
            self.assertNotIn("release_approval_receipts", state)

    def test_release_approval_only_fails_closed_for_unauthorized_actor(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            _state(path)
            with mock.patch.object(panel, "_read_secret_file", side_effect=_secret), \
                 self.assertRaises(TelegramControlContractError):
                replay.replay_release_approval_only(path, _release_update(user_id=999))
            state = panel.load_state(path)
            self.assertNotIn(replay.SEEN_UPDATES_KEY, state)
            self.assertNotIn("release_approval_receipts", state)

    def test_release_approval_only_duplicate_update_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            _state(path)
            update = _release_update(203)
            with mock.patch.object(panel, "_read_secret_file", side_effect=_secret):
                self.assertTrue(replay.replay_release_approval_only(path, update))
                first = panel.load_state(path)
                self.assertTrue(replay.replay_release_approval_only(path, update))
            second = panel.load_state(path)
            self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()

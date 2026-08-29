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
from scripts.orchestration_telegram_ingress_outbox import ApprovalDecision, ReleaseCandidateDigest, TelegramControlContractError
from scripts.telegram_release_approval import callback_data_for


def _state(path: Path) -> None:
    panel.save_state(path, {
        "schema_version": 1, "telegram_offset": 0, "sessions": {}, "requests": {},
        "pending_actions": [], "production_queue": [], "last_event_at": None,
    })


def _update(update_id: int = 101) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": "cb-1", "from": {"id": 88},
            "message": {"message_id": 42, "chat": {"id": 77}}, "data": "cmd:menu",
        },
    }


def _message_update(update_id: int = 304, *, text: str = "Channel OS", user_id: int = 88, chat_id: int = 77) -> dict:
    return {
        "update_id": update_id,
        "message": {"message_id": 53, "from": {"id": user_id}, "chat": {"id": chat_id}, "text": text},
    }


def _channel_update(update_id: int = 301, *, data: str = "cmd:channelos-refresh", user_id: int = 88, chat_id: int = 77) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"channel-os-{update_id}", "from": {"id": user_id},
            "message": {"message_id": 52, "chat": {"id": chat_id}}, "data": data,
        },
    }


def _candidate() -> ReleaseCandidateDigest:
    return ReleaseCandidateDigest(
        run_id="run-1", final_mp4_sha256="a" * 64, delivery_manifest_sha256="b" * 64,
        capability_manifest_sha256="c" * 64, release_asset_set_digest="d" * 64,
    )


def _release_update(update_id: int = 201, *, user_id: int = 88, chat_id: int = 77) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"approval-{update_id}", "from": {"id": user_id},
            "message": {"message_id": 99, "chat": {"id": chat_id}},
            "data": callback_data_for(_candidate(), ApprovalDecision.APPROVED),
        },
    }


def _secret(name: str, *, required: bool = False) -> str:
    return {
        "TELEGRAM_ALLOWED_USER_ID_FILE": "88",
        "TELEGRAM_CHAT_ID_FILE": "77",
        "TELEGRAM_BOT_TOKEN_FILE": "bot-token",
    }.get(name, "")


class TelegramWebhookReplayTests(unittest.TestCase):
    def test_decode_update_accepts_bounded_callback_json(self):
        encoded = base64.b64encode(json.dumps(_update()).encode()).decode()
        self.assertEqual(replay.decode_update(encoded)["update_id"], 101)

    def test_decode_update_rejects_malformed_or_unsupported_payload(self):
        with self.assertRaises(RuntimeError):
            replay.decode_update("not-base64")
        encoded = base64.b64encode(json.dumps({"update_id": 1}).encode()).decode()
        with self.assertRaises(RuntimeError):
            replay.decode_update(encoded)

    def test_replay_injects_one_update_and_suppresses_second_callback_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"; _state(path)
            observed = {"updates": None, "runs": 0}
            def fake_poll(state_path: Path) -> None:
                observed["runs"] += 1
                client = panel.TelegramClient("token")
                observed["updates"] = client.call("getUpdates", {})
                client.answer_callback("cb-1")
                panel.save_state(state_path, panel.load_state(state_path))
            def original_call(self, method, payload=None):
                raise AssertionError(f"unexpected network call: {method}")
            with mock.patch.object(replay.memory_ui, "_install_policy"), \
                 mock.patch("scripts.telegram_persistent_control_ui.install"), \
                 mock.patch.object(active, "_poll", side_effect=fake_poll), \
                 mock.patch.object(panel.TelegramClient, "call", new=original_call):
                self.assertTrue(replay.replay_update(path, _update()))
            self.assertEqual(observed, {"updates": [_update()], "runs": 1})
            self.assertEqual(panel.load_state(path)[replay.SEEN_UPDATES_KEY], [101])

    def test_duplicate_update_has_no_second_side_effect(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"; _state(path)
            state = panel.load_state(path); state[replay.SEEN_UPDATES_KEY] = [101]; panel.save_state(path, state)
            with mock.patch.object(active, "_poll") as poll:
                self.assertFalse(replay.replay_update(path, _update()))
            poll.assert_not_called()

    def test_out_of_order_unique_updates_are_not_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"; _state(path)
            state = panel.load_state(path); state[replay.SEEN_UPDATES_KEY] = [105]; panel.save_state(path, state)
            def fake_poll(state_path: Path) -> None:
                panel.save_state(state_path, panel.load_state(state_path))
            with mock.patch.object(replay.memory_ui, "_install_policy"), \
                 mock.patch("scripts.telegram_persistent_control_ui.install"), \
                 mock.patch.object(active, "_poll", side_effect=fake_poll):
                self.assertTrue(replay.replay_update(path, _update(104)))
            self.assertEqual(panel.load_state(path)[replay.SEEN_UPDATES_KEY], [105, 104])

    def test_release_approval_only_consumes_approval_without_legacy_poll(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"; _state(path)
            with mock.patch.object(panel, "_read_secret_file", side_effect=_secret), mock.patch.object(active, "_poll") as poll:
                self.assertTrue(replay.replay_release_approval_only(path, _release_update()))
            poll.assert_not_called()
            state = panel.load_state(path)
            self.assertEqual(state[replay.SEEN_UPDATES_KEY], [201])
            self.assertEqual(len(state["release_approval_receipts"]), 1)

    def test_release_approval_only_rejects_non_release_command_without_marking_seen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"; _state(path)
            with mock.patch.object(panel, "_read_secret_file", side_effect=_secret), mock.patch.object(active, "_poll") as poll:
                self.assertFalse(replay.replay_release_approval_only(path, _channel_update(202)))
            poll.assert_not_called()
            state = panel.load_state(path)
            self.assertNotIn(replay.SEEN_UPDATES_KEY, state)
            self.assertNotIn("release_approval_receipts", state)

    def test_release_approval_only_fails_closed_for_unauthorized_actor(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"; _state(path)
            with mock.patch.object(panel, "_read_secret_file", side_effect=_secret), self.assertRaises(TelegramControlContractError):
                replay.replay_release_approval_only(path, _release_update(user_id=999))
            self.assertNotIn(replay.SEEN_UPDATES_KEY, panel.load_state(path))

    def test_release_approval_only_duplicate_update_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"; _state(path); update = _release_update(203)
            with mock.patch.object(panel, "_read_secret_file", side_effect=_secret):
                self.assertTrue(replay.replay_release_approval_only(path, update)); first = panel.load_state(path)
                self.assertTrue(replay.replay_release_approval_only(path, update))
            self.assertEqual(first, panel.load_state(path))

    def test_channel_os_callback_is_rendered_before_legacy_poll_and_marked_seen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"; _state(path)
            keyboard = [[{"text": "Back", "callback_data": "cmd:menu"}]]
            with mock.patch.object(panel, "_read_secret_file", side_effect=_secret), \
                 mock.patch.object(replay, "render_control_state", return_value=("Problems", keyboard)) as render, \
                 mock.patch.object(panel.TelegramClient, "send") as send, mock.patch.object(active, "_poll") as poll:
                self.assertTrue(replay.replay_update(path, _channel_update(301, data="cmd:channelos-problems")))
            poll.assert_not_called(); render.assert_called_once(); send.assert_called_once_with(77, "Problems", keyboard=keyboard)
            self.assertEqual(panel.load_state(path)[replay.SEEN_UPDATES_KEY], [301])

    def test_channel_os_text_entry_is_rendered_before_legacy_parser(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"; _state(path)
            keyboard = [[{"text": "Refresh", "callback_data": "cmd:channelos-refresh"}]]
            with mock.patch.object(panel, "_read_secret_file", side_effect=_secret), \
                 mock.patch.object(replay, "render_control_state", return_value=("Mission Control", keyboard)) as render, \
                 mock.patch.object(panel.TelegramClient, "send") as send, mock.patch.object(active, "_poll") as poll:
                self.assertTrue(replay.replay_update(path, _message_update(304, text="لوحة القناة")))
            poll.assert_not_called(); render.assert_called_once(); send.assert_called_once_with(77, "Mission Control", keyboard=keyboard)
            self.assertEqual(panel.load_state(path)[replay.SEEN_UPDATES_KEY], [304])

    def test_channel_os_duplicate_update_never_renders_or_sends_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"; _state(path)
            state = panel.load_state(path); state[replay.SEEN_UPDATES_KEY] = [302]; panel.save_state(path, state)
            with mock.patch.object(replay, "render_control_state") as render, mock.patch.object(panel.TelegramClient, "send") as send:
                self.assertFalse(replay.replay_update(path, _channel_update(302)))
            render.assert_not_called(); send.assert_not_called()

    def test_channel_os_request_fails_closed_for_unauthorized_actor(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"; _state(path)
            with mock.patch.object(panel, "_read_secret_file", side_effect=_secret), \
                 mock.patch.object(panel.TelegramClient, "send") as send, self.assertRaises(RuntimeError):
                replay.replay_update(path, _channel_update(303, user_id=999))
            send.assert_not_called(); self.assertNotIn(replay.SEEN_UPDATES_KEY, panel.load_state(path))

    def test_safe_during_production_keeps_release_approval_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"; _state(path)
            with mock.patch.object(panel, "_read_secret_file", side_effect=_secret), mock.patch.object(active, "_poll") as poll:
                self.assertEqual(replay.replay_safe_during_production(path, _release_update(401)), "release_approval")
            poll.assert_not_called()
            self.assertEqual(panel.load_state(path)[replay.SEEN_UPDATES_KEY], [401])

    def test_safe_during_production_serves_channel_os_without_legacy_parser(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"; _state(path)
            keyboard = [[{"text": "Refresh", "callback_data": "cmd:channelos-refresh"}]]
            with mock.patch.object(panel, "_read_secret_file", side_effect=_secret), \
                 mock.patch.object(replay, "render_control_state", return_value=("Producing", keyboard)) as render, \
                 mock.patch.object(panel.TelegramClient, "send") as send, mock.patch.object(active, "_poll") as poll:
                self.assertEqual(replay.replay_safe_during_production(path, _channel_update(402)), "channel_os")
            poll.assert_not_called(); render.assert_called_once(); send.assert_called_once_with(77, "Producing", keyboard=keyboard)
            self.assertEqual(panel.load_state(path)[replay.SEEN_UPDATES_KEY], [402])

    def test_safe_during_production_rejects_other_stateful_commands_without_marking_seen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"; _state(path)
            with mock.patch.object(panel, "_read_secret_file", side_effect=_secret), \
                 mock.patch.object(active, "_poll") as poll, mock.patch.object(panel.TelegramClient, "send") as send:
                self.assertIsNone(replay.replay_safe_during_production(path, _update(403)))
            poll.assert_not_called(); send.assert_not_called()
            self.assertNotIn(replay.SEEN_UPDATES_KEY, panel.load_state(path))


if __name__ == "__main__":
    unittest.main()

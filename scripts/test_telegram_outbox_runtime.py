from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.orchestration_telegram_ingress_outbox import OutboxStatus, TelegramControlContractError
from scripts.telegram_outbox_runtime import (
    STATE_KEY,
    begin_send,
    enqueue,
    reconcile_absent,
    reconcile_sent,
    send_current,
)

UTC = "2026-08-29T20:00:00+00:00"


def write_secret(root: Path, name: str, value: str) -> Path:
    path = root / name
    path.write_text(value, encoding="utf-8")
    return path


class TelegramOutboxRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state.json"
        self.state.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "telegram_offset": 0,
                    "sessions": {},
                    "requests": {},
                    "pending_actions": [],
                    "last_event_at": None,
                }
            ),
            encoding="utf-8",
        )
        self.request = self.root / "request.json"
        self.request.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "outbox_message_id": "release-approval-1",
                    "message_kind": "release_candidate",
                    "correlation_id": "run-1",
                    "journal_event_ref": "release-candidate:abc",
                    "created_at": UTC,
                    "method": "sendMessage",
                    "payload": {"text": "Approve?", "reply_markup": {"inline_keyboard": []}},
                }
            ),
            encoding="utf-8",
        )
        token = write_secret(self.root, "token", "bot-token")
        chat = write_secret(self.root, "chat", "123")
        self.env = {
            "TELEGRAM_BOT_TOKEN_FILE": str(token),
            "TELEGRAM_CHAT_ID_FILE": str(chat),
        }

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _state_record(self):
        state = json.loads(self.state.read_text(encoding="utf-8"))
        return state[STATE_KEY]["release-approval-1"]

    def _make_reconciliation_required(self) -> None:
        with patch.dict(os.environ, self.env, clear=False):
            enqueue(self.state, self.request)
            begin_send(self.state, "release-approval-1")
            recovered, allowed = begin_send(self.state, "release-approval-1")
        self.assertFalse(allowed)
        self.assertEqual(recovered.status, OutboxStatus.RECONCILIATION_REQUIRED)

    def test_enqueue_persists_pending_before_send(self) -> None:
        with patch.dict(os.environ, self.env, clear=False):
            message = enqueue(self.state, self.request)
        self.assertEqual(message.status, OutboxStatus.PENDING)
        record = self._state_record()
        self.assertEqual(record["message"]["status"], "PENDING")
        self.assertEqual(record["request"]["payload"]["chat_id"], "123")

    def test_enqueue_is_idempotent_but_conflicting_payload_fails(self) -> None:
        with patch.dict(os.environ, self.env, clear=False):
            first = enqueue(self.state, self.request)
            second = enqueue(self.state, self.request)
            self.assertEqual(first, second)
            raw = json.loads(self.request.read_text(encoding="utf-8"))
            raw["payload"]["text"] = "different"
            self.request.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaises(TelegramControlContractError):
                enqueue(self.state, self.request)

    def test_begin_persists_sending_before_network(self) -> None:
        with patch.dict(os.environ, self.env, clear=False):
            enqueue(self.state, self.request)
            message, allowed = begin_send(self.state, "release-approval-1")
        self.assertTrue(allowed)
        self.assertEqual(message.status, OutboxStatus.SENDING)
        self.assertEqual(self._state_record()["message"]["status"], "SENDING")

    def test_restart_from_sending_becomes_reconciliation_required_and_never_resends(self) -> None:
        self._make_reconciliation_required()
        with patch("scripts.telegram_outbox_runtime.requests.post") as post:
            recovered, allowed = begin_send(self.state, "release-approval-1")
        self.assertFalse(allowed)
        self.assertEqual(recovered.status, OutboxStatus.RECONCILIATION_REQUIRED)
        post.assert_not_called()

    def test_reconcile_sent_requires_provider_message_id_and_never_calls_telegram(self) -> None:
        self._make_reconciliation_required()
        with patch("scripts.telegram_outbox_runtime.requests.post") as post:
            with self.assertRaises(TelegramControlContractError):
                reconcile_sent(self.state, "release-approval-1", "")
            sent = reconcile_sent(self.state, "release-approval-1", "777")
        post.assert_not_called()
        self.assertEqual(sent.status, OutboxStatus.SENT)
        self.assertEqual(sent.telegram_message_id, "777")
        self.assertEqual(self._state_record()["message"]["status"], "SENT")

    def test_reconcile_absent_returns_pending_without_provider_call_then_retry_is_explicit(self) -> None:
        self._make_reconciliation_required()
        with patch("scripts.telegram_outbox_runtime.requests.post") as post:
            pending = reconcile_absent(
                self.state,
                "release-approval-1",
                next_retry_at="2026-08-29T20:05:00+00:00",
            )
        post.assert_not_called()
        self.assertEqual(pending.status, OutboxStatus.PENDING)
        self.assertEqual(pending.next_retry_at, "2026-08-29T20:05:00+00:00")
        with patch.dict(os.environ, self.env, clear=False):
            sending, allowed = begin_send(self.state, "release-approval-1")
        self.assertTrue(allowed)
        self.assertEqual(sending.status, OutboxStatus.SENDING)
        self.assertEqual(sending.attempts, 2)

    def test_reconciliation_commands_refuse_non_ambiguous_state(self) -> None:
        with patch.dict(os.environ, self.env, clear=False):
            enqueue(self.state, self.request)
        with self.assertRaises(TelegramControlContractError):
            reconcile_sent(self.state, "release-approval-1", "777")
        with self.assertRaises(TelegramControlContractError):
            reconcile_absent(self.state, "release-approval-1")

    def test_send_marks_sent_only_after_provider_message_id(self) -> None:
        response = type(
            "Response",
            (),
            {
                "raise_for_status": lambda self: None,
                "json": lambda self: {"ok": True, "result": {"message_id": 77}},
            },
        )()
        with patch.dict(os.environ, self.env, clear=False), patch(
            "scripts.telegram_outbox_runtime.requests.post", return_value=response
        ) as post:
            enqueue(self.state, self.request)
            begin_send(self.state, "release-approval-1")
            sent = send_current(self.state, "release-approval-1")
        self.assertEqual(sent.status, OutboxStatus.SENT)
        self.assertEqual(sent.telegram_message_id, "77")
        self.assertEqual(self._state_record()["message"]["status"], "SENT")
        self.assertEqual(post.call_count, 1)

    def test_send_refuses_non_sending_state(self) -> None:
        with patch.dict(os.environ, self.env, clear=False):
            enqueue(self.state, self.request)
            with self.assertRaises(TelegramControlContractError):
                send_current(self.state, "release-approval-1")


if __name__ == "__main__":
    unittest.main()

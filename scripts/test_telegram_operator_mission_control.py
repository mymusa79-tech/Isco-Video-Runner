from __future__ import annotations

import unittest
from unittest import mock

from scripts import telegram_control_active_ui as active
from scripts import telegram_operator_mission_control as mission
from scripts import telegram_production_queue as queue


class _Client:
    def __init__(self) -> None:
        self.sent: list[tuple[object, str, object]] = []

    def send(self, chat_id, text: str, keyboard=None) -> None:
        self.sent.append((chat_id, text, keyboard))


def _target_state() -> dict:
    return {
        "requests": {
            "req-1": {
                "approved_topic": "الانضباط الهادئ",
            }
        },
        active.ACTIVE_RESEARCH_SESSION_KEY: "session-1",
        active.PRODUCTION_TARGET_KEY: {
            "request_id": "req-1",
            "request_sha256": "hash-1",
            "session_id": "session-1",
            "selected_at": "2026-09-01T10:00:00+00:00",
        },
        "production_queue": [],
    }


def _ready_request() -> dict:
    request = {
        "schema_version": 1,
        "request_id": "req-1",
        "source": "telegram_editorial_control_panel",
        "kind": "long",
        "approval_scope": "long_only",
        "approved_topic": "الانضباط الهادئ",
        "approved_at": "2026-09-01T10:00:00+00:00",
        "approved_by_user": True,
        "production_dispatch_authorized": False,
        "status": "approved_waiting_production_activation",
    }
    request["request_sha256"] = queue._request_hash(request)
    return request


class TelegramOperatorMissionControlTests(unittest.TestCase):
    def test_approved_target_is_one_unambiguous_awaiting_confirmation_state(self) -> None:
        snapshot = mission.current_production_state(_target_state())
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.lifecycle, "awaiting_confirmation")
        text = mission.render_operator_status(snapshot)
        self.assertIn("الحالة: 🟡 ينتظر التأكيد", text)
        self.assertIn("Production Run: لم يبدأ بعد", text)
        self.assertIn("req-1", text)
        self.assertIn("الانضباط الهادئ", text)
        self.assertIn("آخر تحديث: 2026-09-01T10:00:00+00:00", text)

    def test_live_dispatch_wins_over_durable_approval_target(self) -> None:
        state = _target_state()
        state["production_queue"] = [
            {
                "request_id": "req-1",
                "status": "pending_dispatch",
                "attempt": 1,
                "requested_at": queue._now(),
            }
        ]
        snapshot = mission.current_production_state(state)
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.lifecycle, "starting")
        text = mission.render_operator_status(snapshot)
        self.assertIn("الحالة: 🔵 بدء التشغيل", text)
        self.assertNotIn("ينتظر التأكيد", text)
        self.assertNotIn("طابور", text)

    def test_consumed_dispatch_is_labeled_as_production_not_diagnostic_run(self) -> None:
        state = _target_state()
        state["production_queue"] = [
            {
                "request_id": "req-1",
                "status": "dispatch_consumed",
                "attempt": 1,
                "consumed_at": queue._now(),
                "workflow_run_id": "123456789",
            }
        ]
        snapshot = mission.current_production_state(state)
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        text = mission.render_operator_status(snapshot)
        self.assertEqual(snapshot.lifecycle, "running")
        self.assertIn("الحالة: 🎬 قيد التشغيل", text)
        self.assertIn("Production Run ID: 123456789", text)
        self.assertNotIn("تشغيل تشخيصي", text)

    def test_terminal_attempt_wins_over_still_durable_target(self) -> None:
        state = _target_state()
        state["production_queue"] = [
            {
                "request_id": "req-1",
                "request_sha256": "hash-1",
                "status": "completed",
                "attempt": 1,
                "completed_at": queue._now(),
                "workflow_run_id": "777",
            }
        ]
        snapshot = mission.current_production_state(state)
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.lifecycle, "completed")
        text = mission.render_operator_status(snapshot)
        self.assertIn("الحالة: ✅ مكتمل", text)
        self.assertIn("Production Run ID: 777", text)
        self.assertNotIn("ينتظر التأكيد", text)

    def test_idempotency_receipts_never_expose_queue_or_retry_window_as_lifecycle(self) -> None:
        cases = (
            ("already_queued", {"request_id": "req-1", "status": "pending_dispatch"}),
            ("already_reserved_recent", {"request_id": "req-1", "status": "dispatch_reserved"}),
            ("already_dispatched_recent", {"request_id": "req-1", "status": "dispatch_consumed", "workflow_run_id": "9"}),
            ("retry_queued", {"request_id": "req-1", "attempt": 2, "status": "pending_dispatch"}),
        )
        for status, action in cases:
            with self.subTest(status=status):
                text = mission._receipt_text(status, action)
                self.assertNotIn("طابور", text)
                self.assertNotIn("نافذة الحماية", text)
                self.assertIn("YouTube: النشر يبقى يدويًا فقط", text)
        retry = mission._receipt_text("retry_queued", cases[-1][1])
        self.assertIn("الحالة: 🔵 بدء التشغيل", retry)
        self.assertIn("حماية التكرار تمنع النسخ المتزامنة فقط", retry)

    def test_completed_request_never_claims_that_a_new_run_started(self) -> None:
        text = mission._receipt_text(
            "already_completed",
            {"request_id": "req-1", "status": "completed"},
        )
        self.assertIn("اكتمل إنتاجه سابقًا", text)
        self.assertNotIn("بدء التشغيل", text)
        self.assertNotIn("لم يُسجّل بعد", text)

    def test_system_status_explicitly_renames_diagnostic_run(self) -> None:
        old = mission._BASE_SYSTEM_STATUS
        try:
            mission._BASE_SYSTEM_STATUS = lambda state, releases: (
                "📋 تفاصيل النظام · Run #14\nالحالة: مكتمل · 100%",
                [[{"text": "↩️", "callback_data": "cmd:status"}]],
            )
            text, _ = mission._system_status({}, [])
        finally:
            mission._BASE_SYSTEM_STATUS = old
        self.assertIn("تشغيل تشخيصي #14", text)
        self.assertIn("هذه ليست حالة Production الحالية", text)
        self.assertNotIn("تفاصيل النظام · Run #14", text)

    def test_confirmation_receipt_delegates_to_existing_certified_handler(self) -> None:
        request = _ready_request()
        state = {
            "requests": {request["request_id"]: request},
            active.ACTIVE_RESEARCH_SESSION_KEY: "session-1",
            active.PRODUCTION_TARGET_KEY: {
                "request_id": request["request_id"],
                "request_sha256": request["request_sha256"],
                "session_id": "session-1",
            },
            "production_queue": [],
        }
        client = _Client()
        old_base = mission._BASE_PRODUCTION_HANDLE
        try:
            mission._BASE_PRODUCTION_HANDLE = active._handle_command
            with mock.patch.object(active, "_production_enabled", return_value=True):
                mission._production_handle("produce_latest", client, state, [], 77)
        finally:
            mission._BASE_PRODUCTION_HANDLE = old_base
        self.assertEqual(len(state["production_queue"]), 1)
        self.assertEqual(state["production_queue"][0]["request_id"], "req-1")
        self.assertEqual(state["production_queue"][0]["status"], "pending_dispatch")
        text = client.sent[-1][1]
        self.assertIn("تم تأكيد الإنتاج", text)
        self.assertIn("الحالة: 🔵 بدء التشغيل", text)
        self.assertNotIn("طابور", text)
        self.assertIn("Production Run: لم يُسجّل بعد", text)

    def test_non_production_commands_delegate_without_receipt_interception(self) -> None:
        calls: list[str] = []
        old_base = mission._BASE_PRODUCTION_HANDLE
        try:
            mission._BASE_PRODUCTION_HANDLE = lambda kind, *args, **kwargs: calls.append(kind)
            mission._production_handle("menu", _Client(), {}, [], 77)
        finally:
            mission._BASE_PRODUCTION_HANDLE = old_base
        self.assertEqual(calls, ["menu"])


if __name__ == "__main__":
    unittest.main()

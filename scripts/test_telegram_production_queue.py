from __future__ import annotations

import unittest

from scripts import telegram_production_queue as queue


def _request(request_id: str = "req-1", *, kind: str = "long") -> dict:
    item = {
        "schema_version": 1,
        "request_id": request_id,
        "source": "telegram_editorial_control_panel",
        "kind": kind,
        "approval_scope": "short_only" if kind == "short" else "long_only",
        "approved_topic": "موضوع اختباري",
        "approved_at": "2026-08-23T07:00:00+00:00",
        "approved_by_user": True,
        "production_dispatch_authorized": False,
        "status": "approved_waiting_production_activation",
    }
    item["request_sha256"] = queue._request_hash(item)
    return item


class TelegramProductionQueueTests(unittest.TestCase):
    def test_latest_ready_request_requires_immutable_approval_contract(self):
        state = {"requests": {"r": _request()}}
        self.assertEqual(queue.latest_ready_request(state)["request_id"], "req-1")
        state["requests"]["r"]["approved_topic"] = "tampered"
        self.assertIsNone(queue.latest_ready_request(state))

    def test_explicit_press_queues_once_with_unique_authorization(self):
        state = {"requests": {"r": _request()}, "production_queue": []}
        status, action = queue.enqueue_latest_request(state, chat_id=77)
        self.assertEqual(status, "queued")
        self.assertEqual(action["status"], "pending_dispatch")
        self.assertEqual(action["chat_id"], 77)
        self.assertEqual(len(action["authorization_id"]), 32)
        second, same = queue.enqueue_latest_request(state, chat_id=77)
        self.assertEqual(second, "already_queued")
        self.assertEqual(same["request_sha256"], action["request_sha256"])
        self.assertEqual(len(state["production_queue"]), 1)

    def test_reservation_is_durable_gate_before_dispatch(self):
        request = _request()
        state = {"requests": {"r": request}, "production_queue": []}
        _, action = queue.enqueue_latest_request(state, chat_id=77)
        reserved = queue.reserve_dispatch(state, action["request_id"], action["request_sha256"])
        self.assertEqual(reserved["status"], "dispatch_reserved")
        self.assertTrue(reserved["reserved_at"])
        self.assertEqual(reserved["authorization_id"], action["authorization_id"])
        self.assertIsNone(queue.pending_dispatch(state))
        status, same = queue.enqueue_latest_request(state, chat_id=77)
        self.assertEqual(status, "already_reserved_recent")
        self.assertEqual(same["authorization_id"], action["authorization_id"])
        self.assertFalse(request["production_dispatch_authorized"])

    def test_mark_dispatched_requires_prior_reservation_and_preserves_stored_request(self):
        request = _request()
        state = {"requests": {"r": request}, "production_queue": []}
        _, action = queue.enqueue_latest_request(state, chat_id=77)
        with self.assertRaisesRegex(RuntimeError, "Reserved"):
            queue.mark_dispatched(state, action["request_id"], action["request_sha256"])
        queue.reserve_dispatch(state, action["request_id"], action["request_sha256"])
        queue.mark_dispatched(state, action["request_id"], action["request_sha256"])
        self.assertEqual(state["production_queue"][0]["status"], "dispatched")
        self.assertFalse(request["production_dispatch_authorized"])
        self.assertEqual(request["status"], "approved_waiting_production_activation")
        status, _ = queue.enqueue_latest_request(state, chat_id=77)
        self.assertEqual(status, "already_dispatched_recent")

    def test_release_tag_is_deterministic_and_kind_scoped(self):
        self.assertEqual(queue.release_tag_for(_request("abc-123")), "video-telegram-abc-123")
        self.assertEqual(queue.release_tag_for(_request("abc-123", kind="short")), "short-telegram-abc-123")

    def test_no_approval_means_no_production_queue(self):
        state = {"requests": {}, "production_queue": []}
        status, action = queue.enqueue_latest_request(state, chat_id=1)
        self.assertEqual(status, "no_ready_request")
        self.assertIsNone(action)
        self.assertEqual(state["production_queue"], [])


if __name__ == "__main__":
    unittest.main()

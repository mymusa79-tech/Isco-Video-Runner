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

    def test_explicit_press_queues_once(self):
        state = {"requests": {"r": _request()}, "production_queue": []}
        status, action = queue.enqueue_latest_request(state, chat_id=77)
        self.assertEqual(status, "queued")
        self.assertEqual(action["status"], "pending_dispatch")
        self.assertEqual(action["chat_id"], 77)
        second, same = queue.enqueue_latest_request(state, chat_id=77)
        self.assertEqual(second, "already_queued")
        self.assertEqual(same["request_sha256"], action["request_sha256"])
        self.assertEqual(len(state["production_queue"]), 1)

    def test_mark_dispatched_preserves_stored_request_as_non_dispatching(self):
        request = _request()
        state = {"requests": {"r": request}, "production_queue": []}
        _, action = queue.enqueue_latest_request(state, chat_id=77)
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

from __future__ import annotations

import unittest

from scripts import telegram_production_queue as queue


def _request(
    request_id: str = "req-1",
    *,
    kind: str = "long",
    approved_at: str = "2026-08-23T07:00:00+00:00",
) -> dict:
    item = {
        "schema_version": 1,
        "request_id": request_id,
        "source": "telegram_editorial_control_panel",
        "kind": kind,
        "approval_scope": "short_only" if kind == "short" else "long_only",
        "approved_topic": "موضوع اختباري",
        "approved_at": approved_at,
        "approved_by_user": True,
        "production_dispatch_authorized": False,
        "status": "approved_waiting_production_activation",
    }
    item["request_sha256"] = queue._request_hash(item)
    return item


class TelegramProductionQueueTests(unittest.TestCase):
    def test_latest_ready_request_requires_immutable_approval_contract(self):
        state = {"requests": {"req-1": _request()}}
        self.assertEqual(queue.latest_ready_request(state)["request_id"], "req-1")
        state["requests"]["req-1"]["approved_topic"] = "tampered"
        self.assertIsNone(queue.latest_ready_request(state))

    def test_exact_request_lookup_requires_matching_id_hash_and_ready_contract(self):
        request = _request("req-exact")
        state = {"requests": {"req-exact": request}}
        self.assertIs(queue.ready_request_by_id(state, "req-exact", request["request_sha256"]), request)
        self.assertIsNone(queue.ready_request_by_id(state, "req-exact", "wrong"))
        self.assertIsNone(queue.ready_request_by_id(state, "missing", request["request_sha256"]))
        request["approved_topic"] = "tampered"
        self.assertIsNone(queue.ready_request_by_id(state, "req-exact", request["request_sha256"]))

    def test_exact_enqueue_never_switches_to_newer_saved_approval(self):
        selected = _request("req-selected", approved_at="2026-08-23T07:00:00+00:00")
        newer = _request("req-newer", approved_at="2026-08-23T08:00:00+00:00")
        state = {
            "requests": {selected["request_id"]: selected, newer["request_id"]: newer},
            "production_queue": [],
        }
        status, action = queue.enqueue_request(
            state,
            selected["request_id"],
            selected["request_sha256"],
            chat_id=77,
        )
        self.assertEqual(status, "queued")
        self.assertEqual(action["request_id"], "req-selected")
        self.assertNotEqual(action["request_id"], queue.latest_ready_request(state)["request_id"])

    def test_exact_enqueue_fails_closed_for_missing_or_wrong_target(self):
        request = _request("req-exact")
        state = {"requests": {"req-exact": request}, "production_queue": []}
        status, action = queue.enqueue_request(state, "req-exact", "wrong", chat_id=77)
        self.assertEqual(status, "no_ready_request")
        self.assertIsNone(action)
        status, action = queue.enqueue_request(state, "req-missing", request["request_sha256"], chat_id=77)
        self.assertEqual(status, "no_ready_request")
        self.assertIsNone(action)
        self.assertEqual(state["production_queue"], [])

    def test_explicit_press_queues_once_with_unique_authorization(self):
        state = {"requests": {"req-1": _request()}, "production_queue": []}
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
        state = {"requests": {"req-1": request}, "production_queue": []}
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

    def test_dedicated_production_authorization_must_be_reserved_then_is_consumed_once(self):
        state = {"requests": {"req-1": _request()}, "production_queue": []}
        _, action = queue.enqueue_latest_request(state, chat_id=77)
        with self.assertRaisesRegex(RuntimeError, "explicit Telegram dispatch authorization"):
            queue.validate_dispatch_authorization(
                state, action["request_id"], action["request_sha256"], action["authorization_id"]
            )
        queue.reserve_dispatch(state, action["request_id"], action["request_sha256"])
        authorized = queue.validate_dispatch_authorization(
            state, action["request_id"], action["request_sha256"], action["authorization_id"]
        )
        self.assertEqual(authorized["status"], "dispatch_reserved")
        consumed = queue.consume_dispatch_authorization(
            state,
            action["request_id"],
            action["request_sha256"],
            action["authorization_id"],
            workflow_run_id="123",
        )
        self.assertEqual(consumed["status"], "dispatch_consumed")
        self.assertEqual(consumed["workflow_run_id"], "123")
        self.assertTrue(consumed["consumed_at"])
        with self.assertRaisesRegex(RuntimeError, "explicit Telegram dispatch authorization"):
            queue.validate_dispatch_authorization(
                state, action["request_id"], action["request_sha256"], action["authorization_id"]
            )
        with self.assertRaisesRegex(RuntimeError, "explicit Telegram dispatch authorization"):
            queue.consume_dispatch_authorization(
                state, action["request_id"], action["request_sha256"], action["authorization_id"]
            )
        status, _ = queue.enqueue_latest_request(state, chat_id=77)
        self.assertEqual(status, "already_dispatched_recent")

    def test_wrong_authorization_cannot_consume_reserved_action(self):
        state = {"requests": {"req-1": _request()}, "production_queue": []}
        _, action = queue.enqueue_latest_request(state, chat_id=77)
        queue.reserve_dispatch(state, action["request_id"], action["request_sha256"])
        with self.assertRaisesRegex(RuntimeError, "explicit Telegram dispatch authorization"):
            queue.consume_dispatch_authorization(
                state, action["request_id"], action["request_sha256"], "wrong-auth"
            )
        self.assertEqual(state["production_queue"][0]["status"], "dispatch_reserved")

    def test_retry_gets_distinct_authorization_and_never_reuses_consumed_attempt(self):
        request = _request()
        state = {"requests": {"req-1": request}, "production_queue": []}
        _, first = queue.enqueue_latest_request(state, chat_id=77)
        queue.reserve_dispatch(state, first["request_id"], first["request_sha256"])
        queue.consume_dispatch_authorization(
            state, first["request_id"], first["request_sha256"], first["authorization_id"]
        )
        state["production_queue"][0]["consumed_at"] = "2026-08-20T00:00:00+00:00"
        status, second = queue.enqueue_latest_request(state, chat_id=77)
        self.assertEqual(status, "retry_queued")
        self.assertNotEqual(first["authorization_id"], second["authorization_id"])
        queue.reserve_dispatch(state, second["request_id"], second["request_sha256"])
        queue.consume_dispatch_authorization(
            state,
            second["request_id"],
            second["request_sha256"],
            second["authorization_id"],
        )
        self.assertEqual(state["production_queue"][0]["status"], "dispatch_consumed")
        self.assertEqual(state["production_queue"][1]["status"], "dispatch_consumed")

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

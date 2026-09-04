from __future__ import annotations

import unittest

from scripts import telegram_used_history_reconcile as reconcile
from scripts.telegram_production_queue import _request_hash, release_tag_for


class TelegramUsedHistoryReconcileTests(unittest.TestCase):
    def _request(self, request_id: str, kind: str, topic: str) -> dict:
        request = {
            "schema_version": 1,
            "request_id": request_id,
            "source": "telegram_editorial_control_panel",
            "kind": kind,
            "approved_topic": topic,
            "format": "moment" if kind == "short" else "auto",
            "approval_scope": "short_only" if kind == "short" else "long_only",
            "approved_by_user": True,
            "approved_at": "2026-08-31T10:00:00+00:00",
            "status": "approved_waiting_production_activation",
            "production_dispatch_authorized": False,
        }
        request["request_sha256"] = _request_hash(request)
        return request

    def _state(self) -> dict:
        request = self._request("req-1", "short", "موضوع مكتمل")
        tag = release_tag_for(request)
        return {
            "requests": {request["request_id"]: request},
            "production_queue": [
                {
                    "request_id": request["request_id"],
                    "request_sha256": request["request_sha256"],
                    "status": "completed",
                    "completed_at": "2026-09-01T12:00:00+00:00",
                    "completed_release_tag": tag,
                }
            ],
            "saved_suggestions": [
                {
                    "archive_id": "archive-1",
                    "status": "available",
                    "kind": "short",
                    "candidate": {"title": "موضوع مكتمل"},
                }
            ],
            "used_topics": [],
        }

    def test_completed_receipt_backfills_used_and_removes_stale_saved_copy(self):
        state = self._state()
        result = reconcile.reconcile_completed_history(state)
        self.assertEqual(result, {"processed": 1, "added": 1})
        self.assertEqual(len(state["used_topics"]), 1)
        self.assertEqual(state["used_topics"][0]["request_id"], "req-1")
        self.assertEqual(state["used_topics"][0]["used_at"], "2026-09-01T12:00:00+00:00")
        self.assertEqual(state["saved_suggestions"], [])

    def test_reconciliation_is_idempotent(self):
        state = self._state()
        reconcile.reconcile_completed_history(state)
        first = list(state["used_topics"])
        result = reconcile.reconcile_completed_history(state)
        self.assertEqual(result, {"processed": 1, "added": 0})
        self.assertEqual(state["used_topics"], first)

    def test_failed_or_inflight_receipts_are_not_promoted_to_used(self):
        state = self._state()
        state["production_queue"][0]["status"] = "failed"
        result = reconcile.reconcile_completed_history(state)
        self.assertEqual(result, {"processed": 0, "added": 0})
        self.assertEqual(state["used_topics"], [])

    def test_tampered_hash_fails_closed(self):
        state = self._state()
        state["production_queue"][0]["request_sha256"] = "tampered"
        with self.assertRaisesRegex(RuntimeError, "hash does not match"):
            reconcile.reconcile_completed_history(state)

    def test_wrong_release_tag_fails_closed(self):
        state = self._state()
        state["production_queue"][0]["completed_release_tag"] = "short-telegram-wrong"
        with self.assertRaisesRegex(RuntimeError, "release tag is not deterministic"):
            reconcile.reconcile_completed_history(state)


if __name__ == "__main__":
    unittest.main()

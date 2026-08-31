from __future__ import annotations

import unittest
from datetime import datetime, timezone

from scripts import telegram_creator_control_center_v5 as v5
from scripts import telegram_production_queue as queue
from scripts import telegram_status_projection as projection


OLD = "2020-01-01T00:00:00+00:00"


def _recent() -> str:
    return datetime.now(timezone.utc).isoformat()


class TelegramDispatchLiveLeaseTests(unittest.TestCase):
    def test_stale_pending_and_reserved_entries_are_history_not_live_queue(self) -> None:
        state = {
            "production_queue": [
                {"status": "pending_dispatch", "requested_at": OLD},
                {"status": "dispatch_reserved", "reserved_at": OLD},
            ]
        }
        self.assertEqual(queue.live_dispatch_count(state), 0)
        self.assertEqual(queue.live_production_dispatches(state), [])

    def test_recent_reserved_entry_remains_live(self) -> None:
        item = {"status": "dispatch_reserved", "reserved_at": _recent()}
        state = {"production_queue": [item]}
        self.assertTrue(queue.dispatch_entry_is_live(item))
        self.assertEqual(queue.live_dispatch_count(state), 1)
        self.assertEqual(queue.live_production_dispatches(state), [item])

    def test_consumed_entry_has_bounded_v4_execution_lease(self) -> None:
        recent = {"status": "dispatch_consumed", "consumed_at": _recent(), "workflow_run_id": "123"}
        stale = {"status": "dispatch_consumed", "consumed_at": OLD, "workflow_run_id": "122"}
        state = {"production_queue": [stale, recent]}
        self.assertFalse(queue.dispatch_entry_is_live(stale))
        self.assertTrue(queue.dispatch_entry_is_live(recent))
        self.assertEqual(queue.live_dispatch_count(state), 0)
        self.assertEqual(queue.live_production_dispatches(state), [recent])

    def test_public_projection_does_not_report_expired_ledger_entries_as_live(self) -> None:
        state = {
            "production_queue": [
                {"status": "pending_dispatch", "requested_at": OLD},
                {"status": "dispatch_reserved", "reserved_at": OLD},
                {"status": "dispatch_consumed", "consumed_at": OLD},
            ]
        }
        editorial = projection.build_projection(state)["editorial"]
        self.assertEqual(editorial["production_queue_count"], 0)
        self.assertEqual(editorial["production_waiting_count"], 0)
        self.assertEqual(editorial["production_reserved_count"], 0)
        self.assertEqual(editorial["production_inflight_count"], 0)

    def test_creator_status_uses_same_live_lease_contract(self) -> None:
        stale = {"status": "dispatch_reserved", "reserved_at": OLD, "attempt": 1}
        recent = {
            "status": "dispatch_consumed",
            "consumed_at": _recent(),
            "attempt": 2,
            "workflow_run_id": "456",
        }
        self.assertIsNone(v5._latest_active_production({"production_queue": [stale]}))
        self.assertIs(v5._latest_active_production({"production_queue": [stale, recent]}), recent)


if __name__ == "__main__":
    unittest.main()

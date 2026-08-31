from __future__ import annotations

import unittest
from datetime import datetime, timezone

from scripts.telegram_status_projection import build_projection


class TelegramStatusProjectionTests(unittest.TestCase):
    def test_waiting_reserved_and_inflight_are_distinct_from_terminal_history(self):
        now = datetime.now(timezone.utc)
        recent = now.isoformat()
        state = {
            "last_event_at": recent,
            "requests": {},
            "production_queue": [
                {"status": "pending_dispatch", "request_id": "req-wait", "requested_at": recent},
                {"status": "dispatch_reserved", "request_id": "req-reserved", "reserved_at": recent},
                {"status": "dispatch_consumed", "request_id": "req-running", "consumed_at": recent},
                {"status": "completed", "request_id": "req-done"},
                {"status": "failed", "request_id": "req-failed"},
            ],
        }

        projection = build_projection(state, generated_at=now)["editorial"]

        # Backward-compatible queue depth is only live work awaiting V4 ownership.
        self.assertEqual(projection["production_queue_count"], 2)
        self.assertEqual(projection["production_waiting_count"], 1)
        self.assertEqual(projection["production_reserved_count"], 1)
        # Consumed is running/in-flight, not queued.
        self.assertEqual(projection["production_inflight_count"], 1)

    def test_terminal_ledger_only_reports_zero_live_production(self):
        state = {
            "production_queue": [
                {"status": "completed", "request_id": "req-done"},
                {"status": "failed", "request_id": "req-failed"},
            ]
        }
        editorial = build_projection(state)["editorial"]
        self.assertEqual(editorial["production_queue_count"], 0)
        self.assertEqual(editorial["production_waiting_count"], 0)
        self.assertEqual(editorial["production_reserved_count"], 0)
        self.assertEqual(editorial["production_inflight_count"], 0)


if __name__ == "__main__":
    unittest.main()

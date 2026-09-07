from __future__ import annotations

import unittest

from scripts import telegram_gold_resume_control as gold
from scripts import telegram_production_queue as production

RUNNER_SHA = "a" * 40
ENGINE_SHA = "b" * 40
FINAL_SHA = "c" * 64


def _request(request_id: str = "req-gold") -> dict:
    item = {
        "schema_version": 1,
        "request_id": request_id,
        "source": "telegram_editorial_control_panel",
        "kind": "long",
        "approval_scope": "long_only",
        "approved_topic": "موضوع Gold اختباري",
        "approved_at": "2026-09-07T20:00:00+00:00",
        "approved_by_user": True,
        "production_dispatch_authorized": False,
        "status": "approved_waiting_production_activation",
    }
    item["request_sha256"] = production._request_hash(item)
    return item


def _state(*, pending: bool = True) -> dict:
    request = _request()
    entry = {
        "schema_version": 1,
        "request_id": request["request_id"],
        "request_sha256": request["request_sha256"],
        "kind": "long",
        "approval_scope": "long_only",
        "authorization_id": "1" * 32,
        "status": "qc_pending" if pending else "failed",
    }
    if pending:
        entry["qc_pending"] = {
            "source_run_id": "12345",
            "source_run_attempt": "1",
            "artifact_name": "isco-qc-pending-12345-1",
            "runner_sha": RUNNER_SHA,
            "engine_sha": ENGINE_SHA,
            "final_sha256": FINAL_SHA,
            "format": "film",
        }
    return {
        "requests": {request["request_id"]: request},
        "production_queue": [entry],
        "gold_resume_queue": [],
    }


class TelegramGoldResumeControlTests(unittest.TestCase):
    def test_only_qc_pending_source_can_create_gold_resume_authorization(self):
        state = _state(pending=True)
        status, action = gold.enqueue_gold_resume(state, "req-gold", chat_id=77)
        self.assertEqual(status, "requested")
        self.assertEqual(action["status"], "pending_dispatch")
        self.assertEqual(action["source_run_id"], "12345")
        self.assertEqual(action["artifact_name"], "isco-qc-pending-12345-1")
        self.assertEqual(action["final_sha256"], FINAL_SHA)
        self.assertEqual(len(action["authorization_id"]), 32)

        with self.assertRaisesRegex(RuntimeError, "not in QC_PENDING"):
            gold.enqueue_gold_resume(_state(pending=False), "req-gold", chat_id=77)

    def test_duplicate_press_never_creates_parallel_gold_resume(self):
        state = _state()
        _, first = gold.enqueue_gold_resume(state, "req-gold", chat_id=77)
        status, second = gold.enqueue_gold_resume(state, "req-gold", chat_id=77)
        self.assertEqual(status, "already_requested")
        self.assertIs(first, second)
        self.assertEqual(len(state["gold_resume_queue"]), 1)

    def test_reservation_is_bound_to_current_runner_and_exact_pending_source(self):
        state = _state()
        _, action = gold.enqueue_gold_resume(state, "req-gold", chat_id=77)
        reserved = gold.reserve_gold_resume(
            state,
            action["request_id"],
            action["authorization_id"],
            runner_sha=RUNNER_SHA,
        )
        self.assertEqual(reserved["status"], "dispatch_reserved")
        self.assertEqual(reserved["dispatch_runner_sha"], RUNNER_SHA)
        with self.assertRaisesRegex(RuntimeError, "different Runner SHA"):
            gold.validate_gold_resume_authorization(
                state,
                action["request_id"],
                action["authorization_id"],
                runner_sha="d" * 40,
            )

    def test_source_identity_drift_blocks_reservation(self):
        state = _state()
        _, action = gold.enqueue_gold_resume(state, "req-gold", chat_id=77)
        state["production_queue"][0]["qc_pending"]["final_sha256"] = "d" * 64
        with self.assertRaisesRegex(RuntimeError, "source identity changed"):
            gold.reserve_gold_resume(
                state,
                action["request_id"],
                action["authorization_id"],
                runner_sha=RUNNER_SHA,
            )
        self.assertEqual(action["status"], "pending_dispatch")

    def test_consumed_resume_is_one_time_and_tracks_workflow_run(self):
        state = _state()
        _, action = gold.enqueue_gold_resume(state, "req-gold", chat_id=77)
        gold.reserve_gold_resume(
            state,
            action["request_id"],
            action["authorization_id"],
            runner_sha=RUNNER_SHA,
        )
        consumed = gold.consume_gold_resume(
            state,
            action["request_id"],
            action["authorization_id"],
            runner_sha=RUNNER_SHA,
            workflow_run_id="999",
        )
        self.assertEqual(consumed["status"], "dispatch_consumed")
        self.assertEqual(consumed["workflow_run_id"], "999")
        with self.assertRaisesRegex(RuntimeError, "Exact Gold resume authorization"):
            gold.validate_gold_resume_authorization(
                state,
                action["request_id"],
                action["authorization_id"],
                runner_sha=RUNNER_SHA,
            )

    def test_completed_resume_is_idempotent_but_source_production_stays_qc_pending_until_reconciled(self):
        state = _state()
        _, action = gold.enqueue_gold_resume(state, "req-gold", chat_id=77)
        gold.reserve_gold_resume(state, "req-gold", action["authorization_id"], runner_sha=RUNNER_SHA)
        gold.consume_gold_resume(
            state,
            "req-gold",
            action["authorization_id"],
            runner_sha=RUNNER_SHA,
            workflow_run_id="999",
        )
        completed = gold.mark_gold_resume_completed(
            state,
            "req-gold",
            action["authorization_id"],
            release_tag="video-telegram-req-gold",
        )
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(state["production_queue"][0]["status"], "qc_pending")
        again = gold.mark_gold_resume_completed(
            state,
            "req-gold",
            action["authorization_id"],
            release_tag="video-telegram-req-gold",
        )
        self.assertIs(again, completed)


if __name__ == "__main__":
    unittest.main()

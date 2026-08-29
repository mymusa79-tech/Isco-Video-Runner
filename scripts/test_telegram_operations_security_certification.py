from __future__ import annotations

import copy
import unittest

from scripts import telegram_publish_gate as gate
from scripts.orchestration_telegram_ingress_outbox import (
    ApprovalDecision,
    ReleaseCandidateDigest,
    TelegramControlContractError,
)
from scripts.telegram_release_approval import (
    approval_projection,
    callback_data_for,
    decision_from_projection,
    record_webhook_approval,
)


def _candidate(run_id: str = "run-1") -> ReleaseCandidateDigest:
    return ReleaseCandidateDigest(
        run_id=run_id,
        final_mp4_sha256="a" * 64,
        delivery_manifest_sha256="b" * 64,
        capability_manifest_sha256="c" * 64,
        release_asset_set_digest="d" * 64,
    )


def _callback(
    candidate: ReleaseCandidateDigest,
    *,
    decision: ApprovalDecision = ApprovalDecision.APPROVED,
    user_id: int = 555,
    chat_id: int = 777,
    update_id: int = 1,
) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb-{update_id}",
            "data": callback_data_for(candidate, decision),
            "from": {"id": user_id},
            "message": {"message_id": 42, "chat": {"id": chat_id}},
        },
    }


class TelegramOperationsSecurityCertificationTests(unittest.TestCase):
    def test_1_stale_candidate_button_cannot_authorize_current_candidate(self) -> None:
        stale = _candidate("run-old")
        current = _candidate("run-current")
        state: dict = {}
        record_webhook_approval(
            state,
            update=_callback(stale, update_id=10),
            allowed_user_id="555",
            allowed_chat_id="777",
            decided_at="2026-08-29T20:00:00+00:00",
        )
        projection = {"release_approvals": approval_projection(state)}
        self.assertEqual(decision_from_projection(projection, stale), ApprovalDecision.APPROVED)
        self.assertIsNone(decision_from_projection(projection, current))

    def test_2_duplicate_authorized_press_has_one_effective_decision(self) -> None:
        candidate = _candidate()
        state: dict = {}
        first = record_webhook_approval(
            state,
            update=_callback(candidate, update_id=10),
            allowed_user_id="555",
            allowed_chat_id="777",
            decided_at="2026-08-29T20:00:00+00:00",
        )
        second = record_webhook_approval(
            state,
            update=_callback(candidate, update_id=11),
            allowed_user_id="555",
            allowed_chat_id="777",
            decided_at="2026-08-29T20:01:00+00:00",
        )
        self.assertEqual(first, second)
        self.assertEqual(first.update_id, 10)
        self.assertEqual(len(state["release_approval_receipts"]), 1)
        projection = {"release_approvals": approval_projection(state)}
        self.assertEqual(decision_from_projection(projection, candidate), ApprovalDecision.APPROVED)

    def test_3_unauthorized_press_is_rejected_and_state_is_unchanged(self) -> None:
        candidate = _candidate()
        state = {"release_approval_receipts": {}}
        before = copy.deepcopy(state)
        with self.assertRaises(TelegramControlContractError):
            record_webhook_approval(
                state,
                update=_callback(candidate, user_id=999),
                allowed_user_id="555",
                allowed_chat_id="777",
            )
        self.assertEqual(state, before)

    def test_4_malformed_or_unknown_callback_causes_no_effective_action(self) -> None:
        candidate = _candidate()
        for data in ("", "approve", "unknown:run-1", "approve:run-1:extra"):
            with self.subTest(data=data):
                state: dict = {}
                update = _callback(candidate)
                update["callback_query"]["data"] = data
                before = copy.deepcopy(state)
                if data.startswith("approve:"):
                    with self.assertRaises(TelegramControlContractError):
                        record_webhook_approval(
                            state,
                            update=update,
                            allowed_user_id="555",
                            allowed_chat_id="777",
                        )
                else:
                    result = record_webhook_approval(
                        state,
                        update=update,
                        allowed_user_id="555",
                        allowed_chat_id="777",
                    )
                    self.assertIsNone(result)
                self.assertEqual(state, before)

    def test_5_opposite_decision_for_same_candidate_is_conflict_not_second_action(self) -> None:
        candidate = _candidate()
        state: dict = {}
        record_webhook_approval(
            state,
            update=_callback(candidate, decision=ApprovalDecision.APPROVED, update_id=20),
            allowed_user_id="555",
            allowed_chat_id="777",
        )
        before = copy.deepcopy(state)
        with self.assertRaises(TelegramControlContractError):
            record_webhook_approval(
                state,
                update=_callback(candidate, decision=ApprovalDecision.REJECTED, update_id=21),
                allowed_user_id="555",
                allowed_chat_id="777",
            )
        self.assertEqual(state, before)
        projection = {"release_approvals": approval_projection(state)}
        self.assertEqual(decision_from_projection(projection, candidate), ApprovalDecision.APPROVED)

    def test_6_publish_gate_no_longer_owns_live_telegram_updates(self) -> None:
        for legacy_name in (
            "is_authorized_user",
            "_telegram_api",
            "_prime_offset",
            "_handle_update",
        ):
            self.assertFalse(hasattr(gate, legacy_name), legacy_name)


if __name__ == "__main__":
    unittest.main()

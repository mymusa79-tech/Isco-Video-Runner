import unittest

from scripts.orchestration_journal import CanonicalRunState, RunState, StageState
from scripts.orchestration_telegram_ingress_outbox import (
    ApprovalDecision,
    IngressDisposition,
    IngressMode,
    IngressOwnerDeclaration,
    OutboxLedger,
    OutboxMessage,
    OutboxStatus,
    ReleaseCandidateDigest,
    TelegramControlContractError,
    TelegramIngressCheckpoint,
    TelegramIngressReducer,
    assert_single_ingress_owner,
    bind_release_approval,
    telegram_journal_projection,
)

H = "a" * 64
H2 = "b" * 64
H3 = "c" * 64
H4 = "d" * 64
H5 = "e" * 64
UTC = "2026-08-29T19:00:00+00:00"


class TelegramIngressTests(unittest.TestCase):
    def _owner(self, owner_id="edge-webhook"):
        return IngressOwnerDeclaration(bot_token_hash=H, owner_id=owner_id)

    def test_only_one_owner_per_bot_token(self):
        owner = self._owner()
        assert_single_ingress_owner(owner, owner)
        with self.assertRaises(TelegramControlContractError):
            assert_single_ingress_owner(owner, self._owner("legacy-poller"))

    def test_ingress_mode_is_webhook_only(self):
        self.assertEqual(tuple(IngressMode), (IngressMode.WEBHOOK,))

    def test_duplicate_update_is_idempotent_but_payload_reuse_fails(self):
        reducer = TelegramIngressReducer(self._owner())
        first = reducer.accept(owner_id="edge-webhook", update_id=10, update_payload_hash=H2)
        self.assertEqual(first.disposition, IngressDisposition.ACCEPTED)
        duplicate = reducer.accept(owner_id="edge-webhook", update_id=10, update_payload_hash=H2)
        self.assertEqual(duplicate.disposition, IngressDisposition.DUPLICATE)
        with self.assertRaises(TelegramControlContractError):
            reducer.accept(owner_id="edge-webhook", update_id=10, update_payload_hash=H3)

    def test_stale_unseen_update_is_rejected_and_checkpoint_is_monotonic(self):
        reducer = TelegramIngressReducer(self._owner())
        reducer.accept(owner_id="edge-webhook", update_id=20, update_payload_hash=H2)
        self.assertEqual(reducer.checkpoint.next_update_id, 21)
        with self.assertRaises(TelegramControlContractError):
            reducer.accept(owner_id="edge-webhook", update_id=19, update_payload_hash=H3)

    def test_checkpoint_cannot_be_claimed_by_different_owner(self):
        checkpoint = TelegramIngressCheckpoint(1, H, "edge-webhook", 7, ((7, H2),))
        with self.assertRaises(TelegramControlContractError):
            TelegramIngressReducer(self._owner("other-owner"), checkpoint)


class TelegramOutboxTests(unittest.TestCase):
    def _message(self, *, payload_hash=H2):
        return OutboxMessage.pending(
            outbox_message_id="msg-1",
            bot_token_hash=H,
            chat_id="123",
            message_kind="release_candidate",
            correlation_id="run-1",
            payload_hash=payload_hash,
            journal_event_ref="journal:run-1:42",
            created_at=UTC,
        )

    def test_enqueue_is_idempotent_and_conflicting_identity_fails(self):
        ledger = OutboxLedger()
        message = self._message()
        self.assertEqual(ledger.enqueue(message), message)
        self.assertEqual(ledger.enqueue(message), message)
        with self.assertRaises(TelegramControlContractError):
            ledger.enqueue(self._message(payload_hash=H3))

    def test_interrupted_send_requires_reconciliation_not_blind_resend(self):
        ledger = OutboxLedger()
        ledger.enqueue(self._message())
        sending = ledger.begin_send("msg-1")
        self.assertEqual(sending.status, OutboxStatus.SENDING)
        recovered = ledger.recover_interrupted_send("msg-1")
        self.assertEqual(recovered.status, OutboxStatus.RECONCILIATION_REQUIRED)
        with self.assertRaises(TelegramControlContractError):
            ledger.begin_send("msg-1")

    def test_reconciliation_can_confirm_sent(self):
        ledger = OutboxLedger()
        ledger.enqueue(self._message())
        ledger.begin_send("msg-1")
        ledger.recover_interrupted_send("msg-1")
        sent = ledger.reconcile("msg-1", confirmed_sent_message_id="tg-77")
        self.assertEqual(sent.status, OutboxStatus.SENT)
        self.assertEqual(sent.telegram_message_id, "tg-77")

    def test_reconciliation_can_prove_absence_before_retry(self):
        ledger = OutboxLedger()
        ledger.enqueue(self._message())
        ledger.begin_send("msg-1")
        ledger.recover_interrupted_send("msg-1")
        pending = ledger.reconcile(
            "msg-1",
            confirmed_absent=True,
            next_retry_at="2026-08-29T19:01:00+00:00",
        )
        self.assertEqual(pending.status, OutboxStatus.PENDING)
        self.assertEqual(pending.attempts, 1)


class TelegramApprovalBindingTests(unittest.TestCase):
    def _candidate(self):
        return ReleaseCandidateDigest(
            run_id="run-1",
            final_mp4_sha256=H2,
            delivery_manifest_sha256=H3,
            capability_manifest_sha256=H4,
            release_asset_set_digest=H5,
        )

    def test_approval_is_bound_to_exact_release_candidate_digest(self):
        candidate = self._candidate()
        approval = bind_release_approval(
            candidate,
            supplied_candidate_digest=candidate.digest,
            approval_id="approval-1",
            actor_id="user-1",
            update_id=100,
            decision=ApprovalDecision.APPROVED,
            journal_event_ref="journal:run-1:80",
        )
        self.assertEqual(approval.candidate_digest, candidate.digest)

    def test_historical_approval_cannot_authorize_different_asset_set(self):
        candidate = self._candidate()
        changed = ReleaseCandidateDigest(
            run_id="run-1",
            final_mp4_sha256=H2,
            delivery_manifest_sha256=H3,
            capability_manifest_sha256=H4,
            release_asset_set_digest="f" * 64,
        )
        with self.assertRaises(TelegramControlContractError):
            bind_release_approval(
                changed,
                supplied_candidate_digest=candidate.digest,
                approval_id="approval-old",
                actor_id="user-1",
                update_id=101,
                decision=ApprovalDecision.APPROVED,
                journal_event_ref="journal:run-1:81",
            )

    def test_telegram_status_remains_projection_only(self):
        state = CanonicalRunState(
            run_id="run-1",
            run_state=RunState.RUNNING,
            stage_states={"render": StageState.RUNNING},
            last_sequence=9,
            event_count=9,
        )
        projection = telegram_journal_projection(state)
        self.assertEqual(projection["authority"], "projection_only")
        self.assertEqual(projection["source"], "canonical_journal_reducer")
        self.assertEqual(projection["ingress_authority"], "single_webhook_owner")


if __name__ == "__main__":
    unittest.main()

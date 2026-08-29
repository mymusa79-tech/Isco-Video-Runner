import tempfile
import unittest
from pathlib import Path

from scripts.orchestration_telegram_ingress_outbox import (
    ApprovalDecision,
    ReleaseCandidateDigest,
    TelegramControlContractError,
)
from scripts.telegram_release_approval import (
    approval_id_for_candidate,
    approval_projection,
    build_release_candidate,
    callback_data_for,
    candidate_digest_from_approval_id,
    canonical_release_asset_set_digest,
    decision_from_projection,
    effective_decision_after_timeout,
    parse_callback_data,
    record_webhook_approval,
)


def _candidate(run_id="run-1"):
    return ReleaseCandidateDigest(
        run_id=run_id,
        final_mp4_sha256="a" * 64,
        delivery_manifest_sha256="b" * 64,
        capability_manifest_sha256="c" * 64,
        release_asset_set_digest="d" * 64,
    )


class TelegramReleaseApprovalTests(unittest.TestCase):
    def test_approval_id_encodes_full_digest_and_fits_callback_limit(self):
        candidate = _candidate()
        approval_id = approval_id_for_candidate(candidate)
        self.assertEqual(candidate_digest_from_approval_id(approval_id), candidate.digest)
        self.assertLessEqual(len(callback_data_for(candidate, ApprovalDecision.APPROVED).encode()), 64)
        self.assertLessEqual(len(callback_data_for(candidate, ApprovalDecision.REJECTED).encode()), 64)

    def test_stale_candidate_has_different_approval_id(self):
        self.assertNotEqual(
            approval_id_for_candidate(_candidate("run-1")),
            approval_id_for_candidate(_candidate("run-2")),
        )

    def test_authorized_webhook_records_exact_digest(self):
        candidate = _candidate()
        state = {}
        bound = record_webhook_approval(
            state,
            update={
                "update_id": 7,
                "callback_query": {
                    "data": callback_data_for(candidate, ApprovalDecision.APPROVED),
                    "from": {"id": 11},
                    "message": {"chat": {"id": 22}},
                },
            },
            allowed_user_id="11",
            allowed_chat_id="22",
            decided_at="2026-08-29T19:00:00+00:00",
        )
        self.assertEqual(bound.candidate_digest, candidate.digest)
        projection = {"release_approvals": approval_projection(state)}
        self.assertEqual(decision_from_projection(projection, candidate), ApprovalDecision.APPROVED)

    def test_unauthorized_actor_rejected(self):
        candidate = _candidate()
        with self.assertRaises(TelegramControlContractError):
            record_webhook_approval(
                {},
                update={"update_id": 1, "callback_query": {"data": callback_data_for(candidate, ApprovalDecision.APPROVED), "from": {"id": 99}, "message": {"chat": {"id": 22}}}},
                allowed_user_id="11",
                allowed_chat_id="22",
            )

    def test_wrong_chat_rejected(self):
        candidate = _candidate()
        with self.assertRaises(TelegramControlContractError):
            record_webhook_approval(
                {},
                update={"update_id": 1, "callback_query": {"data": callback_data_for(candidate, ApprovalDecision.APPROVED), "from": {"id": 11}, "message": {"chat": {"id": 99}}}},
                allowed_user_id="11",
                allowed_chat_id="22",
            )

    def test_duplicate_callback_is_idempotent(self):
        candidate = _candidate()
        update = {"update_id": 7, "callback_query": {"data": callback_data_for(candidate, ApprovalDecision.APPROVED), "from": {"id": 11}, "message": {"chat": {"id": 22}}}}
        state = {}
        first = record_webhook_approval(state, update=update, allowed_user_id="11", allowed_chat_id="22", decided_at="2026-08-29T19:00:00+00:00")
        second = record_webhook_approval(state, update=update, allowed_user_id="11", allowed_chat_id="22", decided_at="2026-08-29T19:05:00+00:00")
        self.assertEqual(first, second)
        self.assertEqual(len(state["release_approval_receipts"]), 1)

    def test_conflicting_duplicate_fails(self):
        candidate = _candidate()
        state = {}
        approve = {"update_id": 7, "callback_query": {"data": callback_data_for(candidate, ApprovalDecision.APPROVED), "from": {"id": 11}, "message": {"chat": {"id": 22}}}}
        reject = {"update_id": 8, "callback_query": {"data": callback_data_for(candidate, ApprovalDecision.REJECTED), "from": {"id": 11}, "message": {"chat": {"id": 22}}}}
        record_webhook_approval(state, update=approve, allowed_user_id="11", allowed_chat_id="22")
        with self.assertRaises(TelegramControlContractError):
            record_webhook_approval(state, update=reject, allowed_user_id="11", allowed_chat_id="22")

    def test_projection_mismatch_fails_closed(self):
        candidate = _candidate()
        projection = {"release_approvals": [{"approval_id": approval_id_for_candidate(candidate), "candidate_digest": "e" * 64, "decision": "APPROVED", "decided_at": "x"}]}
        with self.assertRaises(TelegramControlContractError):
            decision_from_projection(projection, candidate)

    def test_timeout_never_approves(self):
        self.assertEqual(effective_decision_after_timeout(), "rejected")

    def test_candidate_requires_real_capability_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "final.mp4").write_bytes(b"video")
            (root / "delivery-manifest.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(TelegramControlContractError):
                build_release_candidate(
                    root=root,
                    run_id="r",
                    capability_manifest_name="capability-manifest.json",
                    release_asset_names=("final.mp4", "delivery-manifest.json"),
                )

    def test_asset_set_digest_is_content_addressed_and_order_independent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a").write_bytes(b"1")
            (root / "b").write_bytes(b"2")
            first = canonical_release_asset_set_digest(root, ("b", "a"))
            self.assertEqual(first, canonical_release_asset_set_digest(root, ("a", "b")))
            (root / "b").write_bytes(b"3")
            self.assertNotEqual(first, canonical_release_asset_set_digest(root, ("a", "b")))

    def test_parse_ignores_non_release_callback(self):
        self.assertIsNone(parse_callback_data("cmd:menu"))


if __name__ == "__main__":
    unittest.main()

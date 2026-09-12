from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import gold_text_qc_pending_v1 as gold_text_pending
from scripts import qc_pending_checkpoint_v1 as checkpoint


class GoldTextTransportClassificationTests(unittest.TestCase):
    def test_semantic_block_is_not_promoted_to_provider_outage(self) -> None:
        semantic = {
            "status": "block",
            "model_review": {"critical_issues": ["real quality issue"]},
        }

        def current(*_args, **_kwargs):
            return semantic, None

        token = gold_text_pending._ACTIVE_RELEASE_REVIEW.set(True)
        try:
            result, error = gold_text_pending._provider_review_with_qc_pending(
                current,
                object(),
                "openrouter",
            )
        finally:
            gold_text_pending._ACTIVE_RELEASE_REVIEW.reset(token)

        self.assertIs(result, semantic)
        self.assertIsNone(error)

    def test_last_text_provider_technical_failure_becomes_typed_outage(self) -> None:
        transport_block = {
            "status": "block",
            "model_review": {"critical_issues": ["Final critic could not complete safely"]},
        }

        def current(*_args, **_kwargs):
            return transport_block, RuntimeError("HTTP 429")

        token = gold_text_pending._ACTIVE_RELEASE_REVIEW.set(True)
        try:
            with self.assertRaises(gold_text_pending.GoldTextProviderMeshUnavailableError):
                gold_text_pending._provider_review_with_qc_pending(
                    current,
                    object(),
                    "openrouter",
                )
        finally:
            gold_text_pending._ACTIVE_RELEASE_REVIEW.reset(token)

    def test_first_provider_technical_failure_still_falls_through_to_existing_fallback(self) -> None:
        transport_block = {"status": "block"}
        technical = RuntimeError("quota")

        def current(*_args, **_kwargs):
            return transport_block, technical

        token = gold_text_pending._ACTIVE_RELEASE_REVIEW.set(True)
        try:
            result, error = gold_text_pending._provider_review_with_qc_pending(
                current,
                object(),
                "gemini",
            )
        finally:
            gold_text_pending._ACTIVE_RELEASE_REVIEW.reset(token)

        self.assertIs(result, transport_block)
        self.assertIs(error, technical)

    def test_outside_gold_release_scope_never_promotes_provider_failure(self) -> None:
        transport_block = {"status": "block"}
        technical = RuntimeError("quota")

        def current(*_args, **_kwargs):
            return transport_block, technical

        result, error = gold_text_pending._provider_review_with_qc_pending(
            current,
            object(),
            "openrouter",
        )
        self.assertIs(result, transport_block)
        self.assertIs(error, technical)


class GoldTextCheckpointTests(unittest.TestCase):
    @staticmethod
    def _sha(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _capture(self, fmt: str) -> tuple[Path, dict, tempfile.TemporaryDirectory]:
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        final = root / "final.mp4"
        final.write_bytes(b"immutable-final-media")
        (root / "final-master-qc.json").write_text("{}", encoding="utf-8")
        (root / "plan.json").write_text(
            json.dumps({"format": fmt}), encoding="utf-8"
        )
        final_sha = self._sha(final)
        acceptance = {
            "acceptance_contract": {
                "contract_id": "final-master-acceptance-test",
                "decision": "pass",
                "sources": {"final": {"sha256": final_sha}},
            }
        }
        record = {"output": root.name, "release_status": "pending"}
        env = {
            "GITHUB_SHA": "a" * 40,
            "ISCO_ENGINE_SHA": "b" * 40,
            "GITHUB_RUN_ID": "245",
            "GITHUB_RUN_ATTEMPT": "1",
        }
        error = gold_text_pending.GoldTextProviderMeshUnavailableError("mesh down")
        with patch.dict(os.environ, env, clear=False), patch.object(
            checkpoint, "require_final_master_acceptance", return_value=acceptance
        ):
            document = checkpoint.capture_qc_pending_checkpoint(
                root,
                error,
                production_record=record,
                output_key=root.name,
            )
        self.assertIsNotNone(document)
        return root, document, tmp

    def test_gold_text_outage_is_resumable_for_all_canonical_formats(self) -> None:
        for fmt in ("film", "story", "moment"):
            with self.subTest(fmt=fmt):
                root, document, tmp = self._capture(fmt)
                try:
                    self.assertEqual(
                        document["status"], "GOLD_TEXT_PENDING_PROVIDER_CAPACITY"
                    )
                    self.assertEqual(
                        document["failure_taxonomy"],
                        "GoldTextProviderMeshUnavailableError",
                    )
                    self.assertEqual(document["format"], fmt)
                    self.assertFalse(document["release_allowed"])
                    self.assertTrue(document["resumable"])
                    self.assertFalse(document["retry_policy"]["rerender_required"])
                    self.assertFalse(document["retry_policy"]["replanning_allowed"])
                    self.assertFalse(document["retry_policy"]["tts_allowed"])
                finally:
                    tmp.cleanup()

    def test_exact_final_sha_is_revalidated_for_gold_text_resume(self) -> None:
        root, document, tmp = self._capture("moment")
        try:
            acceptance = {
                "acceptance_contract": {
                    "contract_id": "final-master-acceptance-test",
                    "decision": "pass",
                    "sources": {"final": {"sha256": document["final"]["sha256"]}},
                }
            }
            with patch.object(
                checkpoint, "require_final_master_acceptance", return_value=acceptance
            ):
                verified = checkpoint.verify_qc_pending_checkpoint(root)
            self.assertEqual(
                verified["status"], "GOLD_TEXT_PENDING_PROVIDER_CAPACITY"
            )

            (root / "final.mp4").write_bytes(b"mutated-after-final-master")
            with patch.object(
                checkpoint, "require_final_master_acceptance", return_value=acceptance
            ):
                with self.assertRaisesRegex(RuntimeError, "hash mismatch"):
                    checkpoint.verify_qc_pending_checkpoint(root)
        finally:
            tmp.cleanup()

    def test_untyped_error_is_not_resumable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertIsNone(
                checkpoint.capture_qc_pending_checkpoint(
                    root,
                    RuntimeError("all providers unavailable"),
                    production_record={"output": root.name},
                    output_key=root.name,
                )
            )


if __name__ == "__main__":
    unittest.main()

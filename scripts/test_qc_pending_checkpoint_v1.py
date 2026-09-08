from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import vision_stage_contract_v2 as vision_contract
from scripts.qc_pending_checkpoint_v1 import (
    CONTRACT_ID,
    CONTRACT_VERSION,
    capture_qc_pending_checkpoint,
    verify_qc_pending_checkpoint,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class QCPendingCheckpointV1Tests(unittest.TestCase):
    OUTPUT_KEY = "output/test/final.mp4"

    def _root(self) -> Path:
        root = Path(tempfile.mkdtemp(prefix="qc-pending-v1-"))
        (root / "final.mp4").write_bytes(b"exact-final-bytes")
        (root / "final-master-qc.json").write_text("{}", encoding="utf-8")
        (root / "plan.json").write_text(json.dumps({"format": "moment"}), encoding="utf-8")
        (root / "gold-enforce-report.json").write_text(
            json.dumps(
                {
                    "same_render": {"artifact_divergence": False},
                    "gold": {"accepted": False},
                }
            ),
            encoding="utf-8",
        )
        (root / "production-failure-diagnostics.json").write_text(
            json.dumps({"failure": "gold"}), encoding="utf-8"
        )
        return root

    def _record(self) -> dict:
        return {
            "created_at": "2026-09-07T20:00:00+00:00",
            "topic": "test",
            "format": "moment",
            "hook": "hook",
            "output": self.OUTPUT_KEY,
        }

    def _acceptance(self, root: Path) -> dict:
        return {
            "acceptance_contract": {
                "contract_id": "final.master.acceptance.v2",
                "decision": "pass",
                "sources": {"final": {"sha256": _sha(root / "final.mp4")}},
            }
        }

    def _mesh_error(self) -> BaseException:
        return vision_contract.legacy.VisionProviderMeshUnavailableError("mesh exhausted")

    def _capture(self, root: Path, acceptance: dict | None = None) -> dict:
        with patch(
            "scripts.qc_pending_checkpoint_v1.require_final_master_acceptance",
            return_value=acceptance or self._acceptance(root),
        ), patch.dict(
            "os.environ",
            {
                "GITHUB_SHA": "a" * 40,
                "ISCO_ENGINE_SHA": "b" * 40,
                "GITHUB_RUN_ID": "12345",
                "GITHUB_RUN_ATTEMPT": "2",
                "GITHUB_REF": "refs/heads/main",
            },
            clear=False,
        ):
            report = capture_qc_pending_checkpoint(
                root,
                self._mesh_error(),
                production_record=self._record(),
                output_key=self.OUTPUT_KEY,
            )
        self.assertIsNotNone(report)
        return report

    def test_mesh_exhaustion_captures_fail_closed_exact_sha_and_state(self) -> None:
        root = self._root()
        report = self._capture(root)
        self.assertEqual(report["contract_id"], CONTRACT_ID)
        self.assertEqual(report["schema_version"], CONTRACT_VERSION)
        self.assertFalse(report["release_allowed"])
        self.assertTrue(report["resumable"])
        self.assertEqual(report["failure_taxonomy"], "VisionProviderMeshUnavailableError")
        self.assertEqual(report["final"]["sha256"], _sha(root / "final.mp4"))
        self.assertEqual(report["production_state"]["output_key"], self.OUTPUT_KEY)
        self.assertEqual(report["production_state"]["record"]["output"], self.OUTPUT_KEY)
        self.assertEqual(report["source_run_id"], "12345")
        diagnostics = json.loads(
            (root / "production-failure-diagnostics.json").read_text(encoding="utf-8")
        )
        self.assertEqual(diagnostics["qc_pending_checkpoint"]["contract_id"], CONTRACT_ID)
        self.assertNotIn("record", diagnostics["qc_pending_checkpoint"]["production_state"])

    def test_outer_duplicate_capture_revalidates_existing_checkpoint(self) -> None:
        root = self._root()
        original = self._capture(root)
        with patch(
            "scripts.qc_pending_checkpoint_v1.require_final_master_acceptance",
            return_value=self._acceptance(root),
        ):
            repeated = capture_qc_pending_checkpoint(root, self._mesh_error())
        self.assertEqual(repeated, original)

    def test_missing_pre_gold_record_refuses_resumable_marker(self) -> None:
        root = self._root()
        with patch(
            "scripts.qc_pending_checkpoint_v1.require_final_master_acceptance",
            return_value=self._acceptance(root),
        ):
            with self.assertRaisesRegex(RuntimeError, "production record is missing"):
                capture_qc_pending_checkpoint(
                    root,
                    self._mesh_error(),
                    production_record=None,
                    output_key=self.OUTPUT_KEY,
                )

    def test_already_accepted_record_is_refused(self) -> None:
        root = self._root()
        record = self._record()
        record["release_status"] = "accepted_after_final_critic"
        with patch(
            "scripts.qc_pending_checkpoint_v1.require_final_master_acceptance",
            return_value=self._acceptance(root),
        ):
            with self.assertRaisesRegex(RuntimeError, "already accepted"):
                capture_qc_pending_checkpoint(
                    root,
                    self._mesh_error(),
                    production_record=record,
                    output_key=self.OUTPUT_KEY,
                )

    def test_similarly_worded_runtime_error_never_creates_pending_marker(self) -> None:
        root = self._root()
        report = capture_qc_pending_checkpoint(
            root,
            RuntimeError("Gold vision provider mesh unavailable due to capacity"),
        )
        self.assertIsNone(report)
        self.assertFalse((root / "qc-pending.json").exists())

    def test_unrelated_failure_never_creates_pending_marker(self) -> None:
        root = self._root()
        report = capture_qc_pending_checkpoint(root, RuntimeError("semantic quality block"))
        self.assertIsNone(report)
        self.assertFalse((root / "qc-pending.json").exists())

    def test_verify_rejects_mutated_final(self) -> None:
        root = self._root()
        acceptance = self._acceptance(root)
        self._capture(root, acceptance)
        (root / "final.mp4").write_bytes(b"mutated")
        with patch(
            "scripts.qc_pending_checkpoint_v1.require_final_master_acceptance",
            return_value=acceptance,
        ):
            with self.assertRaisesRegex(RuntimeError, "hash mismatch"):
                verify_qc_pending_checkpoint(root)

    def test_verify_rejects_mutated_production_record(self) -> None:
        root = self._root()
        acceptance = self._acceptance(root)
        self._capture(root, acceptance)
        path = root / "qc-pending.json"
        report = json.loads(path.read_text(encoding="utf-8"))
        report["production_state"]["record"]["topic"] = "tampered"
        path.write_text(json.dumps(report), encoding="utf-8")
        with patch(
            "scripts.qc_pending_checkpoint_v1.require_final_master_acceptance",
            return_value=acceptance,
        ):
            with self.assertRaisesRegex(RuntimeError, "production record hash mismatch"):
                verify_qc_pending_checkpoint(root)


if __name__ == "__main__":
    unittest.main()
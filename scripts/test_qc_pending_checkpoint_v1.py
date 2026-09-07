from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.qc_pending_checkpoint_v1 import (
    CONTRACT_ID,
    capture_qc_pending_checkpoint,
    verify_qc_pending_checkpoint,
)


class VisionProviderMeshUnavailableError(RuntimeError):
    pass


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class QCPendingCheckpointV1Tests(unittest.TestCase):
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

    def _acceptance(self, root: Path) -> dict:
        return {
            "acceptance_contract": {
                "contract_id": "final.master.acceptance.v2",
                "decision": "pass",
                "sources": {"final": {"sha256": _sha(root / "final.mp4")}},
            }
        }

    def test_mesh_exhaustion_captures_fail_closed_exact_sha(self) -> None:
        root = self._root()
        with patch(
            "scripts.qc_pending_checkpoint_v1.require_final_master_acceptance",
            return_value=self._acceptance(root),
        ):
            report = capture_qc_pending_checkpoint(root, VisionProviderMeshUnavailableError("mesh"))
        self.assertIsNotNone(report)
        self.assertEqual(report["contract_id"], CONTRACT_ID)
        self.assertFalse(report["release_allowed"])
        self.assertEqual(report["final"]["sha256"], _sha(root / "final.mp4"))
        diagnostics = json.loads(
            (root / "production-failure-diagnostics.json").read_text(encoding="utf-8")
        )
        self.assertEqual(diagnostics["qc_pending_checkpoint"]["contract_id"], CONTRACT_ID)

    def test_unrelated_failure_never_creates_pending_marker(self) -> None:
        root = self._root()
        report = capture_qc_pending_checkpoint(root, RuntimeError("semantic quality block"))
        self.assertIsNone(report)
        self.assertFalse((root / "qc-pending.json").exists())

    def test_verify_rejects_mutated_final(self) -> None:
        root = self._root()
        acceptance = self._acceptance(root)
        with patch(
            "scripts.qc_pending_checkpoint_v1.require_final_master_acceptance",
            return_value=acceptance,
        ):
            capture_qc_pending_checkpoint(root, VisionProviderMeshUnavailableError("mesh"))
        (root / "final.mp4").write_bytes(b"mutated")
        with patch(
            "scripts.qc_pending_checkpoint_v1.require_final_master_acceptance",
            return_value=acceptance,
        ):
            with self.assertRaisesRegex(RuntimeError, "hash mismatch"):
                verify_qc_pending_checkpoint(root)


if __name__ == "__main__":
    unittest.main()

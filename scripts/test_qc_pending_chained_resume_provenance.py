from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import vision_stage_contract_v2 as vision_contract
from scripts.qc_pending_checkpoint_v1 import capture_qc_pending_checkpoint


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class QCPendingChainedResumeProvenanceTests(unittest.TestCase):
    OUTPUT_KEY = "output/chained/final.mp4"

    def _root(self) -> Path:
        root = Path(tempfile.mkdtemp(prefix="qc-pending-chained-"))
        (root / "final.mp4").write_bytes(b"same-parent-final")
        (root / "final-master-qc.json").write_text("{}", encoding="utf-8")
        (root / "plan.json").write_text(json.dumps({"format": "moment"}), encoding="utf-8")
        (root / "gold-enforce-report.json").write_text(
            json.dumps({"same_render": {"artifact_divergence": False}, "gold": {"accepted": False}}),
            encoding="utf-8",
        )
        return root

    def test_audio_resume_then_gold_pending_preserves_original_media_creator(self) -> None:
        root = self._root()
        original_runner = "1" * 40
        original_engine = "2" * 40
        resume_runner = "3" * 40
        runtime_engine = "4" * 40
        acceptance = {
            "acceptance_contract": {
                "contract_id": "final.master.acceptance.v2",
                "decision": "pass",
                "sources": {"final": {"sha256": _sha(root / "final.mp4")}},
            }
        }
        env = {
            "GITHUB_SHA": resume_runner,
            "ISCO_ENGINE_SHA": runtime_engine,
            "GITHUB_RUN_ID": "88001",
            "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_REF": "refs/heads/main",
            "ISCO_SOURCE_RUNNER_SHA": original_runner,
            "ISCO_SOURCE_ENGINE_SHA": original_engine,
            "ISCO_SOURCE_RUN_ID": "77001",
            "ISCO_SOURCE_RUN_ATTEMPT": "2",
            "ISCO_SOURCE_PRODUCTION_ID": "v4:77001:2",
        }
        error = vision_contract.legacy.VisionProviderMeshUnavailableError("mesh exhausted")
        with patch(
            "scripts.qc_pending_checkpoint_v1.require_final_master_acceptance",
            return_value=acceptance,
        ), patch.dict(os.environ, env, clear=False):
            report = capture_qc_pending_checkpoint(
                root,
                error,
                production_record={
                    "output": self.OUTPUT_KEY,
                    "release_status": "pending",
                    "topic": "source",
                },
                output_key=self.OUTPUT_KEY,
            )

        self.assertEqual(report["runner_sha"], original_runner)
        self.assertEqual(report["engine_sha"], original_engine)
        self.assertEqual(report["source_run_id"], "77001")
        self.assertEqual(report["source_run_attempt"], "2")
        self.assertEqual(report["source_identity_origin"], "prior_post_render_resume")
        self.assertEqual(report["checkpoint_execution"]["runner_sha"], resume_runner)
        self.assertEqual(report["checkpoint_execution"]["engine_sha"], runtime_engine)
        self.assertEqual(report["checkpoint_execution"]["run_id"], "88001")
        self.assertFalse(report["checkpoint_execution"]["parent_media_rebuilt"])

    def test_partial_source_override_is_refused(self) -> None:
        root = self._root()
        acceptance = {
            "acceptance_contract": {
                "contract_id": "final.master.acceptance.v2",
                "decision": "pass",
                "sources": {"final": {"sha256": _sha(root / "final.mp4")}},
            }
        }
        error = vision_contract.legacy.VisionProviderMeshUnavailableError("mesh exhausted")
        env = {
            "GITHUB_SHA": "3" * 40,
            "ISCO_ENGINE_SHA": "4" * 40,
            "GITHUB_RUN_ID": "88001",
            "GITHUB_RUN_ATTEMPT": "1",
            "ISCO_SOURCE_RUNNER_SHA": "1" * 40,
            "ISCO_SOURCE_ENGINE_SHA": "",
            "ISCO_SOURCE_RUN_ID": "",
            "ISCO_SOURCE_RUN_ATTEMPT": "",
        }
        with patch(
            "scripts.qc_pending_checkpoint_v1.require_final_master_acceptance",
            return_value=acceptance,
        ), patch.dict(os.environ, env, clear=False):
            with self.assertRaisesRegex(RuntimeError, "source identity is partial"):
                capture_qc_pending_checkpoint(
                    root,
                    error,
                    production_record={"output": self.OUTPUT_KEY, "release_status": "pending"},
                    output_key=self.OUTPUT_KEY,
                )


if __name__ == "__main__":
    unittest.main()

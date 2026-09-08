from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import vision_stage_contract_v2 as vision_contract
from scripts.qc_pending_checkpoint_v1 import capture_qc_pending_checkpoint
from scripts.qc_pending_resume_bundle_v1 import build_resume_bundle, validate_resume_bundle


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class QCPendingResumeBundleV1Tests(unittest.TestCase):
    def _source(self, *, fmt: str = "film") -> tuple[Path, dict]:
        root = Path(tempfile.mkdtemp(prefix="qc-resume-source-"))
        (root / "final.mp4").write_bytes(b"exact-video" * 500)
        documents = {
            "plan.json": {"format": fmt, "topic": "resume"},
            "quality-final.json": {"format": fmt, "duration_ok": True, "audio_ok": True},
            "visual-audit.json": [{"status": "pass", "is_selected": True}],
            "rights-manifest.json": {"visuals": [{"provider": "pexels", "provider_asset_id": 1}]},
            "monetization-check.json": {"status": "PASS"},
            "factuality-audit.json": {"status": "pass"},
            "content-quality-audit.json": {"status": "pass"},
            "tone-quality-audit.json": {"status": "pass"},
            "opening-visual-audit.json": {"status": "pass"},
            "final-master-qc.json": {},
            "ai-budget.json": {"format": fmt},
            "production-failure-diagnostics.json": {"failure": "gold"},
        }
        if fmt == "moment":
            documents["short-intelligence-pre-gold.json"] = {
                "stage": "pre_gold",
                "short_template": "why_reframe",
                "timed_text_events": [{"text": "hook", "start": 0, "end": 1}],
            }
        for name, value in documents.items():
            (root / name).write_text(json.dumps(value), encoding="utf-8")
        acceptance = {
            "acceptance_contract": {
                "contract_id": "final.master.acceptance.v2",
                "decision": "pass",
                "sources": {"final": {"sha256": _sha(root / "final.mp4")}},
            }
        }
        record = {
            "created_at": "2026-09-07T20:00:00+00:00",
            "topic": "resume",
            "format": fmt,
            "output": "output/resume/final.mp4",
        }
        with patch(
            "scripts.qc_pending_checkpoint_v1.require_final_master_acceptance",
            return_value=acceptance,
        ), patch.dict(
            "os.environ",
            {
                "GITHUB_SHA": "a" * 40,
                "ISCO_ENGINE_SHA": "b" * 40,
                "GITHUB_RUN_ID": "99123",
                "GITHUB_RUN_ATTEMPT": "1",
                "GITHUB_REF": "refs/heads/main",
            },
            clear=False,
        ):
            capture_qc_pending_checkpoint(
                root,
                vision_contract.legacy.VisionProviderMeshUnavailableError("capacity"),
                production_record=record,
                output_key=record["output"],
            )
        return root, acceptance

    def test_bundle_round_trip_binds_source_and_every_required_file(self) -> None:
        source, acceptance = self._source()
        bundle = Path(tempfile.mkdtemp(prefix="qc-resume-parent-")) / "bundle"
        with patch(
            "scripts.qc_pending_checkpoint_v1.require_final_master_acceptance",
            return_value=acceptance,
        ):
            manifest = build_resume_bundle(source, bundle)
            verified = validate_resume_bundle(
                bundle,
                expected_source_run_id="99123",
                expected_runner_sha="a" * 40,
                expected_engine_sha="b" * 40,
            )
        self.assertEqual(manifest, verified)
        self.assertFalse(manifest["release_allowed"])
        self.assertFalse(manifest["parent_media_rebuild_allowed"])
        self.assertEqual(manifest["source"]["run_id"], "99123")
        self.assertEqual(manifest["final_sha256"], _sha(bundle / "final.mp4"))
        self.assertIn("qc-pending.json", manifest["files"])
        self.assertIn("factuality-audit.json", manifest["files"])
        self.assertIn("content-quality-audit.json", manifest["files"])
        self.assertIn("tone-quality-audit.json", manifest["files"])

    def test_moment_bundle_requires_pre_gold_short_intelligence(self) -> None:
        source, acceptance = self._source(fmt="moment")
        bundle = Path(tempfile.mkdtemp(prefix="qc-resume-short-")) / "bundle"
        with patch(
            "scripts.qc_pending_checkpoint_v1.require_final_master_acceptance",
            return_value=acceptance,
        ):
            manifest = build_resume_bundle(source, bundle)
        self.assertIn("short-intelligence-pre-gold.json", manifest["files"])
        (bundle / "short-intelligence-pre-gold.json").unlink()
        with patch(
            "scripts.qc_pending_checkpoint_v1.require_final_master_acceptance",
            return_value=acceptance,
        ):
            with self.assertRaisesRegex(RuntimeError, "bundle file missing: short-intelligence-pre-gold.json"):
                validate_resume_bundle(bundle)

    def test_mutated_video_is_rejected_before_gold(self) -> None:
        source, acceptance = self._source()
        bundle = Path(tempfile.mkdtemp(prefix="qc-resume-parent-")) / "bundle"
        with patch(
            "scripts.qc_pending_checkpoint_v1.require_final_master_acceptance",
            return_value=acceptance,
        ):
            build_resume_bundle(source, bundle)
        (bundle / "final.mp4").write_bytes(b"tampered")
        with patch(
            "scripts.qc_pending_checkpoint_v1.require_final_master_acceptance",
            return_value=acceptance,
        ):
            with self.assertRaisesRegex(RuntimeError, "hash mismatch: final.mp4"):
                validate_resume_bundle(bundle)

    def test_wrong_source_run_is_rejected(self) -> None:
        source, acceptance = self._source()
        bundle = Path(tempfile.mkdtemp(prefix="qc-resume-parent-")) / "bundle"
        with patch(
            "scripts.qc_pending_checkpoint_v1.require_final_master_acceptance",
            return_value=acceptance,
        ):
            build_resume_bundle(source, bundle)
            with self.assertRaisesRegex(RuntimeError, "source run id mismatch"):
                validate_resume_bundle(bundle, expected_source_run_id="99124")


if __name__ == "__main__":
    unittest.main()

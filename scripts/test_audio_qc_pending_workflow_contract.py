from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import resume_audio_qc_pending_v1 as resume


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_WORKFLOW = ROOT / ".github" / "workflows" / "produce-resilient-v4.yml"
RESUME_WORKFLOW = ROOT / ".github" / "workflows" / "resume-audio-qc-pending.yml"


class AudioQCPendingWorkflowContractTests(unittest.TestCase):
    def test_failed_production_artifact_retains_exact_resume_inputs(self) -> None:
        text = PRODUCTION_WORKFLOW.read_text(encoding="utf-8")
        required = (
            "engine/output/*/final.mp4",
            "engine/output/*/plan.json",
            "engine/output/*/quality-final.json",
            "engine/output/*/visual-audit.json",
            "engine/output/*/rights-manifest.json",
            "engine/output/*/monetization-check.json",
            "engine/output/*/audio-producer-repair.json",
            "engine/output/*/audio-retention-qc.json",
            "engine/output/*/audio-production-contract-v2.json",
            "engine/output/*/audio-semantic-resume-state.json",
            "engine/output/*/audio-qc-pending.json",
            "engine/output/*/audio/*.wav",
            "engine/output/*/narration.wav",
            "engine/output/*/short-intelligence-pre-gold.json",
            "engine/output/*/short-visual-timeline.json",
        )
        for value in required:
            with self.subTest(value=value):
                self.assertIn(value, text)

    def test_resume_workflow_is_exact_attempt_single_use_and_never_reproduces_parent(self) -> None:
        text = RESUME_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("source_run_id:", text)
        self.assertIn("source_run_attempt:", text)
        self.assertIn("qc_pending_resume_source_gate.py", text)
        self.assertIn("audio_qc_pending_resume_bundle_v1.py build", text)
        self.assertIn("audio-qc-resume-consumed-${SOURCE_RUN_ID}-${SOURCE_RUN_ATTEMPT}", text)
        self.assertIn("resume_audio_qc_pending_v1.py", text)
        self.assertIn("--runtime-runner-sha \"$GITHUB_SHA\"", text)
        self.assertIn("--runtime-engine-sha \"$EXPECTED_ENGINE_SHA\"", text)
        self.assertIn("qc_pending_resume_bundle_v1.py build", text)
        self.assertIn("--destination \"$bundle\"", text)
        self.assertIn("--target-sha \"$GITHUB_SHA\"", text)
        self.assertNotIn("run_v3_voice.py", text)
        self.assertNotIn("run_telegram_control_production.py", text)
        self.assertNotIn("orchestrator.produce", text)

    def test_resume_workflow_retains_and_validates_inherited_budget_evidence(self) -> None:
        text = RESUME_WORKFLOW.read_text(encoding="utf-8")
        for value in (
            "ai-budget.json",
            "ai-budget-audio-resume.json",
            "audio-resume-budget-envelope.json",
            "source_budget_inherited",
            "combined_provider_attempts_after_gold",
            "Audio resume exceeded inherited provider hard cap",
        ):
            with self.subTest(value=value):
                self.assertIn(value, text)

    def test_memory_persistence_has_encryption_key_scope(self) -> None:
        text = RESUME_WORKFLOW.read_text(encoding="utf-8")
        marker = "- name: Persist accepted cross-run memory after release success"
        start = text.index(marker)
        block = text[start : start + 700]
        self.assertIn("STATE_ENCRYPTION_KEY: ${{ secrets.STATE_ENCRYPTION_KEY }}", block)
        self.assertIn("persistent_memory.py encrypt", block)

    def test_manual_long_brief_is_revalidated_and_materialized_only_for_continuation(self) -> None:
        brief = {
            "approved_by_user": True,
            "approved_topic": "موضوع",
            "format": "film",
        }
        checkpoint = {
            "ingress": "manual",
            "format": "film",
            "approved_brief": brief,
            "approved_brief_sha256": "a" * 64,
        }
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            resume,
            "verify_brief_approval",
            return_value="a" * 64,
        ) as verifier:
            path = resume._materialize_manual_approved_brief(checkpoint, Path(temp_dir))
            self.assertIsNotNone(path)
            self.assertTrue(path.is_file())
            self.assertIn("موضوع", path.read_text(encoding="utf-8"))
            verifier.assert_called_once_with(brief, "a" * 64)

    def test_telegram_resume_cannot_smuggle_manual_brief_snapshot(self) -> None:
        checkpoint = {
            "ingress": "telegram",
            "format": "moment",
            "approved_brief": {"approved_by_user": True},
            "approved_brief_sha256": "a" * 64,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(
                resume.AudioQCPendingResumeError,
                "telegram_checkpoint_contains_manual_brief",
            ):
                resume._materialize_manual_approved_brief(checkpoint, Path(temp_dir))


if __name__ == "__main__":
    unittest.main()

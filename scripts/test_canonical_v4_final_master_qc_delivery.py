from __future__ import annotations

import unittest
from pathlib import Path


WORKFLOW = Path(".github/workflows/produce-resilient-v4.yml")


class CanonicalV4FinalMasterQCDeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = WORKFLOW.read_text(encoding="utf-8")

    def test_failure_diagnostics_preserve_final_master_qc(self) -> None:
        start = self.text.index("- name: Upload diagnostics on failure")
        end = self.text.index("\n\n      - name: Final review and extract result", start)
        block = self.text[start:end]
        self.assertIn("engine/output/*/final-master-qc.json", block)

    def test_final_review_requires_and_revalidates_exact_qc_report(self) -> None:
        start = self.text.index("- name: Final review and extract result")
        end = self.text.index("\n\n      - name: Upload final delivery bundle", start)
        block = self.text[start:end]
        self.assertIn("final_master_qc = root / 'final-master-qc.json'", block)
        self.assertIn("for required in (final, quality, plan, critic, budget, manifest, final_master_qc):", block)
        self.assertIn("qc.get('status') != 'pass'", block)
        self.assertIn("qc.get('production_stage') != 'post_render_pre_gold_acceptance'", block)
        self.assertIn("qc.get('full_decode_ok') is not True", block)
        self.assertIn("qc.get('final_media_mutated') is not False", block)
        self.assertIn("list(qc.get('blocking_findings') or [])", block)
        self.assertIn("final_master_qc_path=", block)

    def test_success_artifact_includes_exact_reviewed_qc_path(self) -> None:
        start = self.text.index("- name: Upload final delivery bundle")
        end = self.text.index("\n\n      - name: Request publish approval via Telegram", start)
        block = self.text[start:end]
        self.assertIn("${{ steps.final_review.outputs.final_master_qc_path }}", block)

    def test_release_rebinds_and_requires_exact_qc_asset(self) -> None:
        start = self.text.index("- name: Create GitHub Release with unified delivery bundle")
        end = self.text.index("\n\n      - name: Checkout agent-state writer", start)
        block = self.text[start:end]
        self.assertIn("FINAL_MASTER_QC_PATH: ${{ steps.final_review.outputs.final_master_qc_path }}", block)
        self.assertIn("test -n \"$FINAL_MASTER_QC_PATH\"", block)
        self.assertIn("final_master_qc != root / 'final-master-qc.json'", block)
        self.assertIn("for required in (video, quality, critic, budget, manifest, final_master_qc):", block)
        self.assertIn("qc.get('status') != 'pass'", block)
        self.assertIn('release_assets=("$FINAL_VIDEO_PATH"', block)
        self.assertIn('"$FINAL_MASTER_QC_PATH"', block)

    def test_telegram_notification_calls_are_bounded(self) -> None:
        start = self.text.index("- name: Notify Telegram")
        end = self.text.index("\n\n      - name: Remove plaintext production secrets and state", start)
        block = self.text[start:end]
        self.assertEqual(block.count("curl --fail-with-body"), 2)
        self.assertEqual(block.count("--connect-timeout 10"), 2)
        self.assertEqual(block.count("--max-time 35"), 2)


if __name__ == "__main__":
    unittest.main()

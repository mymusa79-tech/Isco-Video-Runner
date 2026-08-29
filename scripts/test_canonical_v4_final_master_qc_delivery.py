from __future__ import annotations

import unittest
from pathlib import Path


WORKFLOW = Path(".github/workflows/produce-resilient-v4.yml")
TELEGRAM_NOTIFY = Path("scripts/telegram_final_notify.py")


class CanonicalV4FinalMasterQCDeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = WORKFLOW.read_text(encoding="utf-8")
        cls.telegram_notify = TELEGRAM_NOTIFY.read_text(encoding="utf-8")

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
        self.assertIn("FINAL_PLAN_PATH: ${{ steps.final_review.outputs.plan_path }}", block)
        self.assertIn("test -n \"$FINAL_MASTER_QC_PATH\"", block)
        self.assertIn("test -n \"$FINAL_PLAN_PATH\"", block)
        self.assertIn("final_master_qc != root / 'final-master-qc.json'", block)
        self.assertIn("plan != root / 'plan.json'", block)
        self.assertIn("capability_manifest = root / 'capability-manifest.json'", block)
        self.assertIn(
            "for required in (video, quality, plan, critic, budget, manifest, final_master_qc, capability_manifest):",
            block,
        )
        self.assertIn("qc.get('status') != 'pass'", block)
        self.assertIn('release_assets=("$FINAL_VIDEO_PATH" "$FINAL_QUALITY_PATH" "$FINAL_PLAN_PATH"', block)
        self.assertIn('"$FINAL_MASTER_QC_PATH"', block)
        self.assertIn('"$FINAL_OUTPUT_ROOT/capability-manifest.json"', block)

    def test_telegram_notification_calls_are_bounded(self) -> None:
        start = self.text.index("- name: Notify Telegram")
        end = self.text.index("\n\n      - name: Remove plaintext production secrets and state", start)
        block = self.text[start:end]
        self.assertIn("continue-on-error: true", block)
        self.assertEqual(block.count("python -m scripts.telegram_final_notify"), 1)
        self.assertNotIn("curl --fail-with-body", block)
        self.assertIn("timeout=35", self.telegram_notify)

        delivery_start = self.telegram_notify.index("def deliver_terminal_message(")
        delivery_end = self.telegram_notify.index("\n\ndef _elapsed_seconds", delivery_start)
        delivery_block = self.telegram_notify[delivery_start:delivery_end]
        fallback_end = delivery_block.index('print("Telegram notify: sendMessage (no saved progress message_id)")')
        progress_message_block = delivery_block[:fallback_end]

        self.assertEqual(
            progress_message_block.count('_telegram_request(token, "editMessageText", edit_payload)'),
            1,
        )
        self.assertEqual(
            progress_message_block.count('_telegram_request(token, "sendMessage", base_payload)'),
            1,
        )
        self.assertIn("Telegram notify: terminal edit failed; bounded sendMessage fallback", progress_message_block)
        self.assertIn("TELEGRAM_TERMINAL_DELIVERY=fallback_sent", progress_message_block)
        self.assertIn("TELEGRAM_TERMINAL_DELIVERY=failed", progress_message_block)
        self.assertEqual(
            delivery_block.count('_telegram_request(token, "sendMessage", base_payload)'),
            2,
        )


if __name__ == "__main__":
    unittest.main()

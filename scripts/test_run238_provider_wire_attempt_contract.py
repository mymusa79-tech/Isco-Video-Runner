from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from scripts import gold_cloudflare_vision_fallback as cloudflare_gold
from scripts import provider_wire_attempt_contract as wire
from scripts import vision_stage_contract_v2 as vision


class Run238ProviderWireAttemptContractRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        wire.install_provider_wire_attempt_contract()

    def test_openrouter_missing_key_fails_before_budget_authorization(self):
        ledger = mock.Mock()
        spec = mock.Mock()
        spec.task_id = "VISUAL_AUDIT_S01"

        with mock.patch.object(vision, "_openrouter_key", return_value=""):
            with self.assertRaises(wire.NoWireVisionStageError) as caught:
                vision._run_openrouter_attempt(
                    ledger,
                    spec,
                    preview=Path("does-not-need-to-exist.mp4"),
                    narration_context="ctx",
                    intended_visual="intent",
                    requested_model=vision.OPENROUTER_PRIMARY_MODEL,
                )

        self.assertIs(caught.exception.wire_attempted, False)
        self.assertIn(wire.NO_WIRE_MARKER, str(caught.exception))
        ledger.authorize.assert_not_called()
        ledger.record_attempt.assert_not_called()

    def test_wire_only_recording_skips_explicit_no_wire_marker(self):
        ledger = mock.Mock()
        spec = mock.Mock()
        spec.task_id = "VISUAL_AUDIT_S01"
        vision._record(
            ledger,
            spec,
            provider="openrouter",
            requested_model="openrouter/free",
            resolved_model="openrouter/free",
            outcome=vision.AttemptOutcome.OTHER,
            detail=f"NoWireVisionStageError: {wire.NO_WIRE_MARKER} local frame failure",
        )
        ledger.record_attempt.assert_not_called()

    def test_gold_frame_preprocessing_failure_happens_before_slot_and_authorization(self):
        cloudflare_gold.reset_attempt_scope()
        ledger = mock.Mock()
        spec = mock.Mock()
        spec.task_id = cloudflare_gold.GOLD_OPENING_TASK_ID
        no_wire = wire.NoWireVisionStageError(
            vision.VisionErrorCode.INTERNAL_CONTRACT_ERROR,
            "frame extraction failed before inference",
            provider="local_preflight",
        )

        with mock.patch.object(cloudflare_gold, "_enabled", return_value=True), \
             mock.patch.object(cloudflare_gold, "_credentials", return_value=("token", "a" * 32)), \
             mock.patch.object(cloudflare_gold, "_prove_workers_free"), \
             mock.patch.object(cloudflare_gold, "_prove_model_access"), \
             mock.patch.object(vision.legacy, "_sample_preview_frames", side_effect=no_wire), \
             mock.patch.object(cloudflare_gold, "_reserve_workflow_call") as reserve, \
             mock.patch.object(vision, "_authorize") as authorize:
            with self.assertRaises(wire.NoWireVisionStageError):
                cloudflare_gold.run_gold_cloudflare_attempt(
                    ledger,
                    spec,
                    preview=Path("broken-preview.mp4"),
                    narration_context="ctx",
                    intended_visual="intent",
                )

        reserve.assert_not_called()
        authorize.assert_not_called()
        ledger.record_attempt.assert_not_called()

    def test_real_wire_failure_is_still_recorded(self):
        ledger = mock.Mock()
        spec = mock.Mock()
        spec.task_id = "VISUAL_AUDIT_S01"
        vision._record(
            ledger,
            spec,
            provider="openrouter",
            requested_model="openrouter/free",
            resolved_model="model-x",
            outcome=vision.AttemptOutcome.RATE_LIMITED,
            detail="HTTP_429 quota exceeded",
        )
        ledger.record_attempt.assert_called_once()


if __name__ == "__main__":
    unittest.main()

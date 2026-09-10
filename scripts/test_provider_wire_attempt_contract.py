from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

import isco_video_agent.orchestrator as orchestrator
from isco_video_agent.ai_budget import (
    BudgetLedger,
    Capability,
    Priority,
    TaskSpec,
    budget_task_scope,
)
from scripts import gold_cloudflare_vision_fallback as cloudflare_gold
from scripts import provider_wire_attempt_contract as wire
from scripts import task_level_planner_router as planner
from scripts import vision_stage_contract_v2 as vision


class _NoWireLocalFailure(RuntimeError):
    wire_attempted = False
    reason_code = "NO_WIRE_LOCAL_FAILURE"


def _task_spec(task_id: str, capability: Capability) -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        kind="RUN238_BOUNDARY_TEST",
        priority=Priority.P0,
        capability=capability,
        max_provider_attempts=1,
        schema_repair_allowed=False,
        local_fallback=False,
        semantic_block_is_final=False,
    )


class Run238ProviderWireAttemptContractRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        wire.install_provider_wire_attempt_contract()

    def test_planning_local_failure_preserves_single_real_attempt_slot(self):
        ledger = BudgetLedger("story", enforce=True)
        spec = _task_spec("RUN238_PLANNING", Capability.TEXT)

        def local_precheck():
            raise _NoWireLocalFailure("NO_WIRE_LOCAL_FAILURE planning precheck")

        with budget_task_scope(ledger, spec, requested_model="model-x"):
            with self.assertRaises(_NoWireLocalFailure):
                planner._budgeted_provider_call("groq", "model-x", local_precheck)
            self.assertEqual(ledger.to_summary()["provider_attempts"]["total"], 0)
            result = planner._budgeted_provider_call(
                "groq",
                "model-x",
                lambda: {"status": "ok"},
            )

        self.assertEqual(result, {"status": "ok"})
        self.assertEqual(ledger.to_summary()["provider_attempts"]["total"], 1)

    def test_direct_engine_local_failure_preserves_single_real_attempt_slot(self):
        ledger = BudgetLedger("story", enforce=True)
        spec = _task_spec("RUN238_DIRECT", Capability.VISION)

        def local_precheck():
            raise _NoWireLocalFailure("NO_WIRE_LOCAL_FAILURE direct precheck")

        with self.assertRaises(_NoWireLocalFailure):
            orchestrator._ledger_call(
                ledger,
                spec,
                "gemini",
                "model-x",
                local_precheck,
            )
        self.assertEqual(ledger.to_summary()["provider_attempts"]["total"], 0)

        result = orchestrator._ledger_call(
            ledger,
            spec,
            "gemini",
            "model-x",
            lambda: "ok",
        )
        self.assertEqual(result, "ok")
        self.assertEqual(ledger.to_summary()["provider_attempts"]["total"], 1)

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

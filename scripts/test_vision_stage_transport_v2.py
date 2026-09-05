from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

import isco_video_agent.orchestrator as orchestrator
from isco_video_agent.ai_budget import Capability, Priority, TaskSpec
from scripts import short_cinematic_director as director
from scripts import vision_stage_contract_v2 as contract
from scripts import vision_stage_transport_v2 as transport


def _preview(temp_dir: str) -> Path:
    path = Path(temp_dir) / "preview.mp4"
    path.write_bytes(b"fake-preview")
    return path


class VisionStageTransportV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        transport._install_transport_boundary()

    def _short_visual_spec(
        self,
        *,
        kind: str = "SHORT_VISUAL_AUDIT",
        capability: Capability = Capability.VISION,
    ) -> TaskSpec:
        return TaskSpec(
            task_id="SHORT_VISUAL_AUDIT_B02_C01",
            kind=kind,
            priority=Priority.P0,
            capability=capability,
            max_provider_attempts=1,
            schema_repair_allowed=False,
            local_fallback=False,
            semantic_block_is_final=False,
        )

    def test_raw_requests_timeout_becomes_provider_transient_stage_error(self) -> None:
        original = getattr(contract._openrouter_call, "_isco_vision_transport_original", None)
        self.assertIsNotNone(original)
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            original.__globals__["requests"], "post", side_effect=requests.Timeout("socket read timeout")
        ), mock.patch.object(
            original.__globals__["legacy"], "_sample_preview_frames", return_value=[b"a", b"b", b"c"]
        ), mock.patch.object(
            original.__globals__["legacy"], "_openrouter_key", return_value="test-key"
        ):
            with self.assertRaises(contract.VisionStageError) as raised:
                contract._openrouter_call(
                    _preview(temp_dir),
                    narration_context="ctx",
                    intended_visual="intent",
                    model="openrouter/free",
                )
        self.assertEqual(raised.exception.code, contract.VisionErrorCode.PROVIDER_TRANSIENT)
        self.assertIn("transport timeout", str(raised.exception))

    def test_raw_connection_error_becomes_provider_transient_stage_error(self) -> None:
        original = getattr(contract._openrouter_call, "_isco_vision_transport_original", None)
        self.assertIsNotNone(original)
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            original.__globals__["requests"], "post", side_effect=requests.ConnectionError("reset")
        ), mock.patch.object(
            original.__globals__["legacy"], "_sample_preview_frames", return_value=[b"a", b"b", b"c"]
        ), mock.patch.object(
            original.__globals__["legacy"], "_openrouter_key", return_value="test-key"
        ):
            with self.assertRaises(contract.VisionStageError) as raised:
                contract._openrouter_call(
                    _preview(temp_dir),
                    narration_context="ctx",
                    intended_visual="intent",
                    model="openrouter/free",
                )
        self.assertEqual(raised.exception.code, contract.VisionErrorCode.PROVIDER_TRANSIENT)
        self.assertIn("connection failure", str(raised.exception))

    def test_402_balance_requirement_is_capacity(self) -> None:
        self.assertEqual(
            transport._classify_http(402, "This request requires at least $1.00 in balance"),
            contract.VisionErrorCode.CAPACITY,
        )

    def test_non_auth_403_is_capacity(self) -> None:
        self.assertEqual(
            transport._classify_http(403, "Provider disabled this route for policy/capacity reasons"),
            contract.VisionErrorCode.CAPACITY,
        )

    def test_auth_403_is_auth_config(self) -> None:
        self.assertEqual(
            transport._classify_http(403, "Invalid API key permission"),
            contract.VisionErrorCode.AUTH_CONFIG,
        )

    def test_transport_installer_is_idempotent(self) -> None:
        once = contract._openrouter_call
        transport._install_transport_boundary()
        self.assertIs(contract._openrouter_call, once)

    def test_short_cinematic_legacy_kind_is_normalized_before_canonical_stage(self) -> None:
        captured: dict[str, object] = {}

        def canonical_stage(ledger, spec, provider, resolved_model, fn, *args, **kwargs):
            del ledger, fn, args, kwargs
            captured.update(
                task_id=spec.task_id,
                kind=spec.kind,
                capability=spec.capability,
                max_provider_attempts=spec.max_provider_attempts,
                provider=provider,
                resolved_model=resolved_model,
            )
            return {"status": "pass"}

        with mock.patch.object(orchestrator, "_ledger_call_status", canonical_stage):
            transport._install_short_visual_audit_kind_bridge()
            result = orchestrator._ledger_call_status(
                None,
                self._short_visual_spec(),
                "gemini",
                "gemini-3.7-flash",
                object(),
            )

        self.assertEqual(result, {"status": "pass"})
        self.assertEqual(captured["task_id"], "SHORT_VISUAL_AUDIT_B02_C01")
        self.assertEqual(captured["kind"], "VISUAL_AUDIT")
        self.assertIs(captured["capability"], Capability.VISION)
        self.assertEqual(captured["max_provider_attempts"], 1)
        self.assertEqual(captured["provider"], "gemini")

    def test_short_cinematic_bridge_does_not_reclassify_unrelated_tasks(self) -> None:
        seen: list[tuple[str, Capability]] = []

        def sink(_ledger, spec, _provider, _model, _fn, *args, **kwargs):
            del args, kwargs
            seen.append((spec.kind, spec.capability))
            return {"status": "pass"}

        with mock.patch.object(orchestrator, "_ledger_call_status", sink):
            transport._install_short_visual_audit_kind_bridge()
            orchestrator._ledger_call_status(
                None,
                self._short_visual_spec(kind="SOME_OTHER_VISION_TASK"),
                "gemini",
                "gemini-3.7-flash",
                object(),
            )
            orchestrator._ledger_call_status(
                None,
                self._short_visual_spec(capability=Capability.TEXT),
                "gemini",
                "gemini-3.7-flash",
                object(),
            )

        self.assertEqual(
            seen,
            [
                ("SOME_OTHER_VISION_TASK", Capability.VISION),
                ("SHORT_VISUAL_AUDIT", Capability.TEXT),
            ],
        )

    def test_short_cinematic_bridge_is_outside_canonical_stage_wrapper(self) -> None:
        source = inspect.getsource(transport.install_vision_provider_reliability)
        canonical_at = source.index("contract.install_vision_provider_reliability()")
        bridge_at = source.index("_install_short_visual_audit_kind_bridge()")
        self.assertLess(canonical_at, bridge_at)

    def test_short_cinematic_reuses_stage_budget_and_semantic_policy(self) -> None:
        source = inspect.getsource(transport._install_run181_route_adapter)
        self.assertIn(
            "contract.VISION_STAGE_SPEC.provider_policy.max_total_inference_attempts",
            source,
        )
        self.assertIn(
            "max_provider_attempts=max(max_attempts, int(spec.max_provider_attempts))",
            source,
        )
        self.assertEqual(
            transport.contract.VISION_STAGE_SPEC.provider_policy.max_total_inference_attempts,
            3,
        )
        self.assertTrue(
            transport.contract.VISION_STAGE_SPEC.provider_policy.semantic_block_is_final
        )

    def test_short_cinematic_keeps_candidate_caps_and_quality_gate_unchanged(self) -> None:
        source = inspect.getsource(director.upgrade_short_cinematic)
        self.assertIn('kind="SHORT_VISUAL_AUDIT"', source)
        self.assertIn("select_with_recovery(", source)
        self.assertIn("_stable_intent_audit", source)
        self.assertEqual(director.MAX_VISION_REVIEWS_PER_ATTEMPT, 1)
        self.assertEqual(director.MAX_VISION_REVIEWS_PER_BEAT, 2)
        self.assertIn(
            "Short cinematic Visual QA could not select a safe distinct asset",
            source,
        )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

import isco_video_agent.orchestrator as orchestrator
from isco_video_agent.ai_budget import BudgetLedger, Capability, Priority, TaskSpec
from scripts import vision_provider_reliability as mesh


_PASS = {
    "status": "pass",
    "relevance": 0.91,
    "visual_quality": 0.88,
    "identifiable_person": False,
    "sensitive_trait_implication_risk": False,
    "prominent_logo_or_brand": False,
    "cultural_conflict": False,
    "cultural_islamic_suitability_risk": False,
    "advertiser_conflict": False,
    "obvious_synthetic_or_visual_artifact": False,
    "reason": "safe and relevant",
}

_BLOCK = {
    **_PASS,
    "status": "block",
    "relevance": 0.20,
    "reason": "not semantically relevant",
}


def _spec(task_id: str, *, priority: Priority = Priority.P0) -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        kind="VISUAL_AUDIT",
        priority=priority,
        capability=Capability.VISION,
        max_provider_attempts=1,
        schema_repair_allowed=False,
        local_fallback=False,
        semantic_block_is_final=False,
    )


def _preview(temp_dir: str) -> Path:
    path = Path(temp_dir) / "preview.mp4"
    path.write_bytes(b"fake-mp4-preview")
    return path


class VisionProviderReliabilityTests(unittest.TestCase):
    def test_semantic_block_never_shops_openrouter(self) -> None:
        ledger = BudgetLedger("film", enforce=True)
        with tempfile.TemporaryDirectory() as temp_dir, mesh.vision_provider_circuit_scope(), mock.patch.object(
            mesh, "_openrouter_visual_audit"
        ) as fallback:
            result = mesh._route_visual_audit(
                ledger,
                _spec("VISUAL_AUDIT_S01_C01"),
                "gemini",
                "gemini-3.7-flash",
                lambda *_args, **_kwargs: dict(_BLOCK),
                "gem-key",
                _preview(temp_dir),
                narration_context="context",
                intended_visual="intent",
                model="gemini-3.7-flash",
            )
        fallback.assert_not_called()
        self.assertEqual(result["status"], "block")
        summary = ledger.to_summary()["provider_attempts"]
        self.assertEqual(summary["total"], 1)
        self.assertEqual(summary["by_provider"], {"gemini": 1})

    def test_gemini_timeout_falls_once_to_openrouter_and_records_real_provider(self) -> None:
        for fmt in ("film", "moment"):
            with self.subTest(format=fmt):
                ledger = BudgetLedger(fmt, enforce=True)
                with tempfile.TemporaryDirectory() as temp_dir, mesh.vision_provider_circuit_scope(), mock.patch.object(
                    mesh,
                    "_openrouter_visual_audit",
                    return_value=(dict(_PASS), mesh.OPENROUTER_VISION_MODELS[0]),
                ) as fallback:
                    result = mesh._route_visual_audit(
                        ledger,
                        _spec("VISUAL_AUDIT_S01_C01"),
                        "gemini",
                        "gemini-3.7-flash",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("Gemini timed out")),
                        "gem-key",
                        _preview(temp_dir),
                        narration_context="context",
                        intended_visual="intent",
                        model="gemini-3.7-flash",
                    )
                self.assertEqual(result["status"], "pass")
                fallback.assert_called_once()
                summary = ledger.to_summary()["provider_attempts"]
                self.assertEqual(summary["total"], 2)
                self.assertEqual(summary["by_provider"], {"gemini": 1, "openrouter": 1})
                self.assertEqual(ledger._tasks["VISUAL_AUDIT_S01_C01"].max_provider_attempts, 2)

    def test_one_gemini_failure_opens_run_circuit_for_later_candidates(self) -> None:
        ledger = BudgetLedger("film", enforce=True)
        primary_calls = []

        def primary(*_args, **_kwargs):
            primary_calls.append("gemini")
            raise TimeoutError("Gemini timed out")

        with tempfile.TemporaryDirectory() as temp_dir, mesh.vision_provider_circuit_scope(), mock.patch.object(
            mesh,
            "_openrouter_visual_audit",
            side_effect=[
                (dict(_PASS), mesh.OPENROUTER_VISION_MODELS[0]),
                (dict(_PASS), mesh.OPENROUTER_VISION_MODELS[0]),
            ],
        ) as fallback:
            preview = _preview(temp_dir)
            first = mesh._route_visual_audit(
                ledger,
                _spec("VISUAL_AUDIT_S01_C01"),
                "gemini",
                "gemini-3.7-flash",
                primary,
                "gem-key",
                preview,
                narration_context="context",
                intended_visual="intent",
            )
            second = mesh._route_visual_audit(
                ledger,
                _spec("VISUAL_AUDIT_S01_C02", priority=Priority.P1),
                "gemini",
                "gemini-3.7-flash",
                primary,
                "gem-key",
                preview,
                narration_context="context",
                intended_visual="intent",
            )
        self.assertEqual(first["status"], "pass")
        self.assertEqual(second["status"], "pass")
        self.assertEqual(primary_calls, ["gemini"])
        self.assertEqual(fallback.call_count, 2)
        summary = ledger.to_summary()["provider_attempts"]
        self.assertEqual(summary["by_provider"], {"gemini": 1, "openrouter": 2})
        self.assertEqual(summary["by_outcome"].get("CIRCUIT_OPEN"), 1)

    def test_both_provider_circuits_fail_fast_without_blind_candidate_retries(self) -> None:
        ledger = BudgetLedger("film", enforce=True)
        primary_calls = []

        def primary(*_args, **_kwargs):
            primary_calls.append("gemini")
            raise TimeoutError("Gemini timed out")

        with tempfile.TemporaryDirectory() as temp_dir, mesh.vision_provider_circuit_scope(), mock.patch.object(
            mesh,
            "_openrouter_visual_audit",
            side_effect=requests.Timeout("OpenRouter timed out"),
        ) as fallback:
            preview = _preview(temp_dir)
            with self.assertRaises(mesh.VisionProviderMeshUnavailableError) as first:
                mesh._route_visual_audit(
                    ledger,
                    _spec("VISUAL_AUDIT_S01_C01"),
                    "gemini",
                    "gemini-3.7-flash",
                    primary,
                    "gem-key",
                    preview,
                    narration_context="context",
                    intended_visual="intent",
                )
            with self.assertRaises(mesh.VisionProviderMeshUnavailableError):
                mesh._route_visual_audit(
                    ledger,
                    _spec("VISUAL_AUDIT_S01_C02", priority=Priority.P1),
                    "gemini",
                    "gemini-3.7-flash",
                    primary,
                    "gem-key",
                    preview,
                    narration_context="context",
                    intended_visual="intent",
                )
        self.assertIn("service_unavailable", str(first.exception))
        self.assertEqual(primary_calls, ["gemini"])
        self.assertEqual(fallback.call_count, 1)
        summary = ledger.to_summary()["provider_attempts"]
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["by_provider"], {"gemini": 1, "openrouter": 1})
        self.assertEqual(summary["by_outcome"].get("CIRCUIT_OPEN"), 2)

    def test_new_production_scope_does_not_inherit_old_circuit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            preview = _preview(temp_dir)
            with mesh.vision_provider_circuit_scope(), mock.patch.object(
                mesh,
                "_openrouter_visual_audit",
                side_effect=requests.Timeout("OpenRouter timed out"),
            ):
                with self.assertRaises(mesh.VisionProviderMeshUnavailableError):
                    mesh._route_visual_audit(
                        None,
                        _spec("VISUAL_AUDIT_S01_C01"),
                        "gemini",
                        "gemini-3.7-flash",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("Gemini timed out")),
                        "gem-key",
                        preview,
                        narration_context="context",
                        intended_visual="intent",
                    )

            with mesh.vision_provider_circuit_scope(), mock.patch.object(
                mesh, "_openrouter_visual_audit"
            ) as fallback:
                result = mesh._route_visual_audit(
                    None,
                    _spec("VISUAL_AUDIT_S01_C01"),
                    "gemini",
                    "gemini-3.7-flash",
                    lambda *_args, **_kwargs: dict(_PASS),
                    "gem-key",
                    preview,
                    narration_context="context",
                    intended_visual="intent",
                )
            self.assertEqual(result["status"], "pass")
            fallback.assert_not_called()

    def test_auth_or_internal_failure_does_not_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, mesh.vision_provider_circuit_scope(), mock.patch.object(
            mesh, "_openrouter_visual_audit"
        ) as fallback:
            with self.assertRaisesRegex(RuntimeError, "401 unauthorized"):
                mesh._route_visual_audit(
                    None,
                    _spec("VISUAL_AUDIT_S01_C01"),
                    "gemini",
                    "gemini-3.7-flash",
                    lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("401 unauthorized")),
                    "gem-key",
                    _preview(temp_dir),
                    narration_context="context",
                    intended_visual="intent",
                )
        fallback.assert_not_called()

    def test_fallback_schema_is_strict_and_uses_engine_threshold_owner(self) -> None:
        malformed = dict(_PASS)
        malformed["cultural_islamic_suitability_risk"] = "false"
        with self.assertRaises(mesh.VisionFallbackSchemaError):
            mesh._validate_visual_contract(malformed)

        extra = dict(_PASS)
        extra["winner"] = True
        with self.assertRaises(mesh.VisionFallbackSchemaError):
            mesh._validate_visual_contract(extra)

        low = dict(_PASS)
        low["status"] = "pass"
        low["relevance"] = 0.64
        normalized = mesh._validate_visual_contract(low)
        self.assertEqual(normalized["status"], "block")

    def test_openrouter_request_uses_free_video_chain_and_exact_json_contract(self) -> None:
        class Response:
            ok = True
            status_code = 200

            def json(self):
                return {
                    "model": mesh.OPENROUTER_VISION_MODELS[0],
                    "choices": [{"message": {"content": json.dumps(_PASS)}}],
                }

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.dict(
            os.environ, {"OPENROUTER_API_KEY": "test-key"}, clear=False
        ), mock.patch.object(mesh.requests, "post", return_value=Response()) as post:
            audit, model = mesh._openrouter_visual_audit(
                _preview(temp_dir),
                narration_context="narration",
                intended_visual="intent",
            )
        self.assertEqual(audit["status"], "pass")
        self.assertEqual(model, mesh.OPENROUTER_VISION_MODELS[0])
        payload = post.call_args.kwargs["json"]
        self.assertEqual(tuple(payload["models"]), mesh.OPENROUTER_VISION_MODELS)
        self.assertTrue(all(item.endswith(":free") for item in payload["models"]))
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertTrue(payload["provider"]["allow_fallbacks"])
        video = payload["messages"][0]["content"][1]
        self.assertEqual(video["type"], "video_url")
        self.assertTrue(video["video_url"]["url"].startswith("data:video/mp4;base64,"))
        self.assertEqual(post.call_args.kwargs["timeout"], mesh.OPENROUTER_TIMEOUT_SECONDS)

    def test_installer_is_idempotent_and_scopes_every_orchestrator_produce(self) -> None:
        states = []

        def fake_status(*_args, **_kwargs):
            return {"status": "pass"}

        def fake_produce(*_args, **_kwargs):
            state = mesh._VISION_CIRCUIT.get()
            self.assertIsNotNone(state)
            states.append(state)
            if len(states) == 1:
                state.gemini_open = True
            else:
                self.assertFalse(state.gemini_open)
            return Path("output/fake")

        with mock.patch.object(orchestrator, "_ledger_call_status", fake_status), mock.patch.object(
            orchestrator, "produce", fake_produce
        ):
            mesh.install_vision_provider_reliability()
            status_once = orchestrator._ledger_call_status
            produce_once = orchestrator.produce
            mesh.install_vision_provider_reliability()
            self.assertIs(orchestrator._ledger_call_status, status_once)
            self.assertIs(orchestrator.produce, produce_once)
            orchestrator.produce()
            orchestrator.produce()
        self.assertEqual(len(states), 2)
        self.assertIsNot(states[0], states[1])
        self.assertTrue(states[0].gemini_open)
        self.assertFalse(states[1].gemini_open)


if __name__ == "__main__":
    unittest.main()

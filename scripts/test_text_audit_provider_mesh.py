from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from isco_video_agent.ai_budget import (
    BudgetLedger,
    Capability,
    Priority,
    TaskSpec,
    budget_task_scope,
)

from scripts import text_audit_provider_mesh as mesh


_FACT_PASS = {
    "status": "pass",
    "unsupported_claims": [],
    "professional_advice_flags": [],
    "expert_persona_flags": [],
    "notes": [],
}

_CONTENT_PASS = {
    "status": "pass",
    "duplicate_groups": [],
    "reason": "",
}


def _spec(kind: str, *, max_attempts: int = 3) -> TaskSpec:
    return TaskSpec(
        task_id=kind,
        kind=kind,
        priority=Priority.P0,
        capability=Capability.TEXT,
        max_provider_attempts=max_attempts,
        schema_repair_allowed=False,
        local_fallback=False,
        semantic_block_is_final=True,
    )


class TextAuditProviderMeshTests(unittest.TestCase):
    def setUp(self):
        mesh._AUDIT_ROUTE_TELEMETRY.clear()

    def test_openrouter_blocked_uses_two_bounded_groq_models(self):
        captured = {}

        def fake_engine_route(providers, prompt, *, cooldown=None):
            captured["names"] = [name for name, _call in providers]
            return mock.Mock(provider="groq:openai/gpt-oss-120b", exhausted=False, attempts=[])

        with mock.patch.object(
            mesh, "_active_groq_pool_tail",
            return_value=("openai/gpt-oss-120b", "qwen/qwen3.8-27b"),
        ), mock.patch.object(
            mesh, "_groq_model_route_eligible", return_value=True
        ), mock.patch.object(
            mesh.run125, "openrouter_preflight_blocked", return_value=True
        ), mock.patch.object(
            mesh.engine_audit_router, "route_text_audit", side_effect=fake_engine_route
        ):
            mesh._mesh_route(
                [("gemini", lambda _p: {}), ("openrouter", lambda _p: {})],
                "audit-prompt",
            )

        self.assertEqual(
            captured["names"],
            [
                "gemini",
                "groq:openai/gpt-oss-120b",
                "groq:qwen/qwen3.8-27b",
            ],
        )

    def test_openrouter_healthy_keeps_total_route_width_three(self):
        captured = {}

        def fake_engine_route(providers, prompt, *, cooldown=None):
            captured["names"] = [name for name, _call in providers]
            return mock.Mock(provider="openrouter", exhausted=False, attempts=[])

        with mock.patch.object(
            mesh, "_active_groq_pool_tail",
            return_value=(
                "openai/gpt-oss-20b",
                "openai/gpt-oss-120b",
                "qwen/qwen3.8-27b",
            ),
        ), mock.patch.object(
            mesh, "_groq_model_route_eligible", return_value=True
        ), mock.patch.object(
            mesh.run125, "openrouter_preflight_blocked", return_value=False
        ), mock.patch.object(
            mesh.engine_audit_router, "route_text_audit", side_effect=fake_engine_route
        ):
            mesh._mesh_route(
                [("gemini", lambda _p: {}), ("openrouter", lambda _p: {})],
                "audit-prompt",
            )

        self.assertEqual(
            captured["names"],
            ["gemini", "groq:openai/gpt-oss-20b", "openrouter"],
        )

    def test_pool_tail_never_returns_model_abandoned_by_planning(self):
        with mock.patch.object(
            mesh.run125,
            "_GROQ_MODEL_POOL",
            ("openai/gpt-oss-20b", "openai/gpt-oss-120b", "qwen/qwen3.8-27b"),
        ), mock.patch.object(
            mesh.run125, "_active_groq_model", return_value="openai/gpt-oss-120b"
        ):
            self.assertEqual(
                mesh._active_groq_pool_tail(),
                ("openai/gpt-oss-120b", "qwen/qwen3.8-27b"),
            )

    def test_first_groq_model_technical_failure_falls_to_second_model(self):
        calls = []

        def gemini(_prompt):
            raise RuntimeError("429 quota exceeded")

        def fake_groq(prompt: str, *, model_name: str):
            calls.append(model_name)
            if model_name.endswith("120b"):
                raise RuntimeError("GROQ_AUDIT_EMPTY_OUTPUT")
            return dict(_CONTENT_PASS)

        with mock.patch.object(
            mesh, "_active_groq_pool_tail",
            return_value=("openai/gpt-oss-120b", "qwen/qwen3.8-27b"),
        ), mock.patch.object(
            mesh, "_groq_model_route_eligible", return_value=True
        ), mock.patch.object(
            mesh, "_groq_audit_json", side_effect=fake_groq
        ), mock.patch.object(
            mesh.run125, "openrouter_preflight_blocked", return_value=True
        ):
            result = mesh._mesh_route(
                [("gemini", gemini), ("openrouter", lambda _p: dict(_CONTENT_PASS))],
                "audit-prompt",
            )

        self.assertFalse(result.exhausted)
        self.assertEqual(result.provider, "groq:qwen/qwen3.8-27b")
        self.assertEqual(
            calls,
            ["openai/gpt-oss-120b", "qwen/qwen3.8-27b"],
        )

    def test_model_specific_rate_limit_does_not_circuit_other_groq_model(self):
        calls = []

        def gemini(_prompt):
            raise RuntimeError("429 quota exceeded")

        def fake_groq(prompt: str, *, model_name: str):
            calls.append(model_name)
            if model_name.endswith("120b"):
                raise RuntimeError("429 rate limit on model")
            return dict(_CONTENT_PASS)

        cooldown = set()
        with mock.patch.object(
            mesh, "_active_groq_pool_tail",
            return_value=("openai/gpt-oss-120b", "qwen/qwen3.8-27b"),
        ), mock.patch.object(
            mesh, "_groq_model_route_eligible", return_value=True
        ), mock.patch.object(
            mesh, "_groq_audit_json", side_effect=fake_groq
        ), mock.patch.object(
            mesh.run125, "openrouter_preflight_blocked", return_value=True
        ):
            result = mesh._mesh_route(
                [("gemini", gemini), ("openrouter", lambda _p: dict(_CONTENT_PASS))],
                "audit-prompt",
                cooldown=cooldown,
            )

        self.assertEqual(result.provider, "groq:qwen/qwen3.8-27b")
        self.assertIn("groq:openai/gpt-oss-120b", cooldown)
        self.assertNotIn("groq:qwen/qwen3.8-27b", cooldown)
        self.assertEqual(len(calls), 2)

    def test_semantic_block_stops_before_second_groq_model(self):
        calls = []

        def gemini(_prompt):
            raise RuntimeError("429 quota exceeded")

        def fake_groq(prompt: str, *, model_name: str):
            calls.append(model_name)
            return {
                "status": "block",
                "duplicate_groups": [["s1", "s2"]],
                "reason": "real semantic verdict",
            }

        with mock.patch.object(
            mesh, "_active_groq_pool_tail",
            return_value=("openai/gpt-oss-120b", "qwen/qwen3.8-27b"),
        ), mock.patch.object(
            mesh, "_groq_model_route_eligible", return_value=True
        ), mock.patch.object(
            mesh, "_groq_audit_json", side_effect=fake_groq
        ), mock.patch.object(
            mesh.run125, "openrouter_preflight_blocked", return_value=True
        ):
            with budget_task_scope(
                BudgetLedger("moment", enforce=True),
                _spec("CONTENT_QUALITY_AUDIT"),
                requested_model="gemini-3.7-flash",
            ):
                result = mesh._mesh_route(
                    [("gemini", gemini), ("openrouter", lambda _p: dict(_CONTENT_PASS))],
                    "audit-prompt",
                )

        self.assertEqual(result.result["status"], "block")
        self.assertEqual(calls, ["openai/gpt-oss-120b"])

    def test_task_bound_validation_falls_through_malformed_first_groq_model(self):
        ledger = BudgetLedger("moment", enforce=True)

        def first_or_second(prompt: str, *, model_name: str):
            if model_name.endswith("120b"):
                return {"status": "pass"}
            return dict(_FACT_PASS)

        with mock.patch.object(
            mesh, "_active_groq_pool_tail",
            return_value=("openai/gpt-oss-120b", "qwen/qwen3.8-27b"),
        ), mock.patch.object(
            mesh, "_groq_model_route_eligible", return_value=True
        ), mock.patch.object(
            mesh, "_groq_audit_json", side_effect=first_or_second
        ), mock.patch.object(
            mesh.run125, "openrouter_preflight_blocked", return_value=True
        ):
            with budget_task_scope(
                ledger,
                _spec("FACTUALITY_AUDIT"),
                requested_model="gemini-3.7-flash",
            ):
                result = mesh._mesh_route(
                    [("gemini", lambda _p: (_ for _ in ()).throw(RuntimeError("timeout"))),
                     ("openrouter", lambda _p: dict(_FACT_PASS))],
                    "audit-prompt",
                )

        self.assertEqual(result.provider, "groq:qwen/qwen3.8-27b")
        outcomes = ledger.to_summary()["provider_attempts"]["by_outcome"]
        self.assertEqual(outcomes.get("SCHEMA_INVALID"), 1)
        self.assertEqual(outcomes.get("SUCCESS"), 1)

    def test_model_specific_route_labels_normalize_to_groq_in_ledger(self):
        ledger = BudgetLedger("moment", enforce=True)
        original_record = mesh.engine_audit_router._record_wire_attempt
        original_marker = getattr(
            mesh.engine_audit_router, "_ISCO_AUDIT_GROQ_MODEL_LEDGER_V1", False
        )
        try:
            mesh.engine_audit_router._ISCO_AUDIT_GROQ_MODEL_LEDGER_V1 = False
            mesh._install_model_aware_ledger_recording()
            with budget_task_scope(
                ledger,
                _spec("FACTUALITY_AUDIT"),
                requested_model="gemini-3.7-flash",
            ):
                mesh.engine_audit_router._record_wire_attempt(
                    "groq:openai/gpt-oss-120b",
                    mesh.engine_audit_router.AttemptOutcome.SUCCESS,
                    duration_seconds=0.1,
                )
        finally:
            mesh.engine_audit_router._record_wire_attempt = original_record
            mesh.engine_audit_router._ISCO_AUDIT_GROQ_MODEL_LEDGER_V1 = original_marker

        summary = ledger.to_summary()
        self.assertEqual(summary["provider_attempts"]["by_provider"].get("groq"), 1)

    def test_route_telemetry_keeps_bounded_error_detail(self):
        class Attempt:
            provider = "groq:openai/gpt-oss-120b"
            outcome = mesh.engine_audit_router.AttemptOutcome.OTHER
            detail = "GROQ_AUDIT_EMPTY_OUTPUT"

        route = mock.Mock(
            provider=None,
            exhausted=True,
            attempts=[Attempt()],
        )
        with budget_task_scope(
            BudgetLedger("moment"),
            _spec("TONE_QUALITY_AUDIT"),
            requested_model="gemini-3.7-flash",
        ):
            mesh._record_route_telemetry(route)

        self.assertEqual(
            mesh._AUDIT_ROUTE_TELEMETRY[-1]["attempts"][0]["detail"],
            "GROQ_AUDIT_EMPTY_OUTPUT",
        )

    def test_attach_text_audit_telemetry_is_durable(self):
        mesh._AUDIT_ROUTE_TELEMETRY.append(
            {
                "task_id": "CONTENT_QUALITY_AUDIT",
                "task_kind": "CONTENT_QUALITY_AUDIT",
                "winner": None,
                "exhausted": True,
                "attempts": [
                    {
                        "provider": "groq:qwen/qwen3.8-27b",
                        "outcome": "other",
                        "detail": "x",
                    }
                ],
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "planning-telemetry.json"
            path.write_text('{"schema_version":2}', encoding="utf-8")
            mesh.attach_text_audit_telemetry(path)
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(
            payload["text_audit_provider_mesh"]["routes"][0]["task_kind"],
            "CONTENT_QUALITY_AUDIT",
        )

    def test_provider_exhaustion_is_technical_unavailable_not_repairable_content(self):
        self.assertTrue(
            mesh._audit_unavailable_dimension(
                "factuality",
                {
                    "status": "block",
                    "unsupported_claims": ["Factuality audit could not be completed safely"],
                },
            )
        )
        self.assertTrue(
            mesh._audit_unavailable_dimension(
                "semantic_repetition",
                {"status": "block", "validation": "providers_exhausted", "duplicate_groups": []},
            )
        )
        self.assertTrue(
            mesh._audit_unavailable_dimension(
                "tone",
                {"status": "block", "validation": "malformed", "naturalness_flags": []},
            )
        )

    def test_real_semantic_block_is_not_provider_unavailable(self):
        self.assertFalse(
            mesh._audit_unavailable_dimension(
                "tone",
                {
                    "status": "block",
                    "validation": "valid",
                    "naturalness_flags": ["generic filler"],
                },
            )
        )

    def test_groq_transport_is_one_explicit_model_and_no_planner_budget_wrapper(self):
        class Response:
            ok = True
            status_code = 200
            headers = {}

            @staticmethod
            def json():
                return {
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": '{"status":"pass","notes":[]}'},
                        }
                    ]
                }

        with tempfile.TemporaryDirectory() as tmp:
            key_path = Path(tmp) / "groq.key"
            key_path.write_text("test-key", encoding="utf-8")
            with mock.patch.dict(
                os.environ,
                {"GROQ_API_KEY_FILE": str(key_path), "RUNNER_TEMP": tmp},
                clear=False,
            ), mock.patch.object(
                mesh, "_groq_request_capacity",
                return_value=(
                    {"estimated_request_tokens": 100},
                    {"action": "admit", "actual_limit": 8000},
                ),
            ), mock.patch.object(
                mesh.capacity, "_proactive_groq_pacing", return_value=0.0
            ), mock.patch.object(
                mesh.capacity, "observe_groq_response", return_value={}
            ), mock.patch.object(
                mesh.planner_router.requests, "post", return_value=Response()
            ), mock.patch.object(
                mesh.planner_router,
                "_budgeted_provider_call",
                side_effect=AssertionError("double accounting"),
            ):
                result = mesh._groq_audit_json(
                    "prompt",
                    model_name="openai/gpt-oss-120b",
                )

        self.assertEqual(result["status"], "pass")


if __name__ == "__main__":
    unittest.main()

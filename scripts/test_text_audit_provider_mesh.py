from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from isco_video_agent.ai_budget import BudgetLedger, Capability, Priority, TaskSpec, budget_task_scope

from scripts import text_audit_provider_mesh as mesh


_FACT_PASS = {
    "status": "pass",
    "unsupported_claims": [],
    "professional_advice_flags": [],
    "expert_persona_flags": [],
    "notes": [],
}


class TextAuditProviderMeshTests(unittest.TestCase):
    def test_inserts_groq_between_gemini_and_openrouter(self):
        captured = {}

        def fake_engine_route(providers, prompt, *, cooldown=None):
            captured["names"] = [name for name, _call in providers]
            captured["prompt"] = prompt
            return "routed"

        with mock.patch.object(mesh, "_groq_route_eligible", return_value=True), mock.patch.object(
            mesh.run125, "openrouter_preflight_blocked", return_value=False
        ), mock.patch.object(mesh.engine_audit_router, "route_text_audit", side_effect=fake_engine_route):
            result = mesh._mesh_route(
                [("gemini", lambda _p: {}), ("openrouter", lambda _p: {})],
                "audit-prompt",
            )

        self.assertEqual(result, "routed")
        self.assertEqual(captured["names"], ["gemini", "groq", "openrouter"])
        self.assertEqual(captured["prompt"], "audit-prompt")

    def test_openrouter_preflight_block_means_no_openrouter_call_surface(self):
        captured = {}

        def fake_engine_route(providers, prompt, *, cooldown=None):
            captured["names"] = [name for name, _call in providers]
            return "routed"

        with mock.patch.object(mesh, "_groq_route_eligible", return_value=True), mock.patch.object(
            mesh.run125, "openrouter_preflight_blocked", return_value=True
        ), mock.patch.object(mesh.engine_audit_router, "route_text_audit", side_effect=fake_engine_route):
            mesh._mesh_route(
                [("gemini", lambda _p: {}), ("openrouter", lambda _p: {})],
                "audit-prompt",
            )

        self.assertEqual(captured["names"], ["gemini", "groq"])

    def test_groq_semantic_block_stops_without_approval_shopping(self):
        openrouter_calls = []

        def gemini(_prompt):
            raise RuntimeError("429 quota exceeded")

        def groq(_prompt):
            return {"status": "block", "flags": ["real semantic verdict"]}

        def openrouter(_prompt):
            openrouter_calls.append(True)
            raise AssertionError("OpenRouter must not run after a real Groq block")

        with mock.patch.object(mesh, "_groq_route_eligible", return_value=True), mock.patch.object(
            mesh, "_groq_audit_json", side_effect=groq
        ), mock.patch.object(mesh.run125, "openrouter_preflight_blocked", return_value=False):
            result = mesh._mesh_route(
                [("gemini", gemini), ("openrouter", openrouter)],
                "audit-prompt",
            )

        self.assertFalse(result.exhausted)
        self.assertEqual(result.provider, "groq")
        self.assertEqual(result.result["status"], "block")
        self.assertEqual(openrouter_calls, [])

    def test_groq_pass_recovers_run155_shape_after_gemini_rate_limit(self):
        openrouter_calls = []

        def gemini(_prompt):
            raise RuntimeError("429 quota exceeded")

        def groq(_prompt):
            return {"status": "pass", "flags": []}

        def openrouter(_prompt):
            openrouter_calls.append(True)
            return {"status": "pass", "flags": []}

        with mock.patch.object(mesh, "_groq_route_eligible", return_value=True), mock.patch.object(
            mesh, "_groq_audit_json", side_effect=groq
        ), mock.patch.object(mesh.run125, "openrouter_preflight_blocked", return_value=False):
            result = mesh._mesh_route(
                [("gemini", gemini), ("openrouter", openrouter)],
                "audit-prompt",
            )

        self.assertFalse(result.exhausted)
        self.assertEqual(result.provider, "groq")
        self.assertEqual(result.result["status"], "pass")
        self.assertEqual(openrouter_calls, [])

    def test_task_bound_validation_falls_through_malformed_gemini_to_groq(self):
        ledger = BudgetLedger("moment", enforce=True)
        spec = TaskSpec(
            task_id="FACTUALITY_AUDIT",
            kind="FACTUALITY_AUDIT",
            priority=Priority.P0,
            capability=Capability.TEXT,
            max_provider_attempts=3,
            schema_repair_allowed=False,
            local_fallback=False,
            semantic_block_is_final=True,
        )

        def malformed_gemini(_prompt):
            return {"status": "pass"}

        with mock.patch.object(mesh, "_groq_route_eligible", return_value=True), mock.patch.object(
            mesh, "_groq_audit_json", return_value=dict(_FACT_PASS)
        ), mock.patch.object(mesh.run125, "openrouter_preflight_blocked", return_value=False):
            with budget_task_scope(ledger, spec, requested_model="gemini-2.5-flash"):
                result = mesh._mesh_route(
                    [("gemini", malformed_gemini), ("openrouter", lambda _p: dict(_FACT_PASS))],
                    "audit-prompt",
                )

        self.assertEqual(result.provider, "groq")
        outcomes = ledger.to_summary()["provider_attempts"]["by_outcome"]
        self.assertEqual(outcomes.get("SCHEMA_INVALID"), 1)
        self.assertEqual(outcomes.get("SUCCESS"), 1)

    def test_three_provider_budget_counts_one_attempt_per_wire_provider(self):
        ledger = BudgetLedger("moment", enforce=True)
        spec = TaskSpec(
            task_id="FACTUALITY_AUDIT",
            kind="FACTUALITY_AUDIT",
            priority=Priority.P0,
            capability=Capability.TEXT,
            max_provider_attempts=3,
            schema_repair_allowed=False,
            local_fallback=False,
            semantic_block_is_final=True,
        )

        def gemini(_prompt):
            raise RuntimeError("timeout")

        with mock.patch.object(mesh, "_groq_route_eligible", return_value=True), mock.patch.object(
            mesh, "_groq_audit_json", return_value=dict(_FACT_PASS)
        ), mock.patch.object(mesh.run125, "openrouter_preflight_blocked", return_value=False):
            with budget_task_scope(ledger, spec, requested_model="gemini-2.5-flash"):
                result = mesh._mesh_route(
                    [("gemini", gemini), ("openrouter", lambda _p: dict(_FACT_PASS))],
                    "audit-prompt",
                )

        summary = ledger.to_summary()
        self.assertEqual(result.provider, "groq")
        self.assertEqual(summary["provider_attempts"]["total"], 2)
        self.assertEqual(summary["provider_attempts"]["by_provider"].get("gemini"), 1)
        self.assertEqual(summary["provider_attempts"]["by_provider"].get("groq"), 1)

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

    def test_real_semantic_block_is_not_misclassified_as_provider_unavailable(self):
        self.assertFalse(
            mesh._audit_unavailable_dimension(
                "factuality",
                {
                    "status": "block",
                    "unsupported_claims": ["Unsupported causal psychological claim"],
                    "professional_advice_flags": [],
                    "expert_persona_flags": [],
                },
            )
        )
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

    def test_groq_audit_transport_does_not_call_planner_budget_wrapper(self):
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
            env = {"GROQ_API_KEY_FILE": str(key_path), "RUNNER_TEMP": tmp}
            with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
                mesh.run125, "_active_groq_model", return_value="openai/gpt-oss-20b"
            ), mock.patch.object(
                mesh, "_groq_request_capacity", return_value=(
                    "openai/gpt-oss-20b",
                    {"estimated_request_tokens": 100},
                    {"action": "admit", "actual_limit": 8000},
                )
            ), mock.patch.object(
                mesh.capacity, "_proactive_groq_pacing", return_value=0.0
            ), mock.patch.object(
                mesh.capacity, "observe_groq_response", return_value={}
            ), mock.patch.object(
                mesh.planner_router.requests, "post", return_value=Response()
            ), mock.patch.object(
                mesh.planner_router, "_budgeted_provider_call", side_effect=AssertionError("double accounting")
            ):
                result = mesh._groq_audit_json("prompt")

        self.assertEqual(result["status"], "pass")


if __name__ == "__main__":
    unittest.main()

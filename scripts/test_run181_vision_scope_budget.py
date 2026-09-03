from __future__ import annotations

import unittest
from unittest import mock

from isco_video_agent.ai_budget import Capability, Priority, TaskSpec
from scripts import provider_health_registry as health
from scripts import run181_vision_mesh_closure as run181
from scripts import task_level_planner_router as planner_router
from scripts import text_audit_provider_mesh as text_mesh
from scripts import vision_provider_reliability as legacy
from scripts import vision_stage_contract_v2 as contract
from scripts import vision_stage_transport_v2 as transport


def _spec(max_attempts: int = 1) -> TaskSpec:
    return TaskSpec(
        task_id="RUN181_SCOPE_BUDGET",
        kind="VISUAL_AUDIT",
        priority=Priority.P0,
        capability=Capability.VISION,
        max_provider_attempts=max_attempts,
        schema_repair_allowed=False,
        local_fallback=False,
        semantic_block_is_final=False,
    )


class Run181VisionScopeBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        health.reset_provider_health()
        run181._GROQ_MODEL_CERTIFIED.set(None)

    def tearDown(self) -> None:
        health.reset_provider_health()
        run181._GROQ_MODEL_CERTIFIED.set(None)

    def _adapter_around(self, fake_route):
        original = contract._route_visual_audit_v2
        contract._route_visual_audit_v2 = fake_route
        transport._install_run181_route_adapter()
        adapter = contract._route_visual_audit_v2
        self.addCleanup(setattr, contract, "_route_visual_audit_v2", original)
        return adapter

    def test_adapter_preserves_v2_three_attempt_taskspec_budget(self) -> None:
        captured = []

        def fake_route(ledger, spec, provider, resolved_model, fn, *args, **kwargs):
            captured.append(spec)
            return {"status": "pass"}

        adapter = self._adapter_around(fake_route)
        with legacy.vision_provider_circuit_scope():
            result = adapter(
                object(),
                _spec(max_attempts=1),
                "gemini",
                "gemini-3.7-flash",
                lambda: None,
            )
        self.assertEqual(result["status"], "pass")
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].max_provider_attempts, 3)
        self.assertEqual(captured[0].task_id, "RUN181_SCOPE_BUDGET")

    def test_adapter_canonicalizes_engine_gemini_alias_before_health_matching(self) -> None:
        seen = []

        def fake_route(ledger, spec, provider, resolved_model, fn, *args, **kwargs):
            seen.append(resolved_model)
            return {"status": "pass"}

        adapter = self._adapter_around(fake_route)
        with legacy.vision_provider_circuit_scope():
            adapter(
                None,
                _spec(),
                "gemini",
                "gemini-2.5-flash",
                lambda: None,
            )
        self.assertEqual(seen, ["gemini-3.7-flash"])

    def test_new_vision_scope_discards_prior_health_and_groq_certification(self) -> None:
        snapshots = []

        def fake_route(ledger, spec, provider, resolved_model, fn, *args, **kwargs):
            snapshots.append(health.snapshot_provider_health())
            return {"status": "pass"}

        adapter = self._adapter_around(fake_route)

        health.publish_provider_unavailable(
            "openrouter",
            model="*",
            quota_domain="*",
            reason="old run",
            source="old_run",
        )
        run181._GROQ_MODEL_CERTIFIED.set(False)

        with legacy.vision_provider_circuit_scope():
            adapter(None, _spec(), "gemini", "gemini-3.7-flash", lambda: None)
            self.assertEqual(snapshots[-1], [])
            self.assertIsNone(run181._GROQ_MODEL_CERTIFIED.get())
            health.publish_provider_unavailable(
                "groq",
                model=run181.GROQ_VISION_MODEL,
                quota_domain=run181.GROQ_VISION_QUOTA_DOMAIN,
                reason="same run",
                source="vision",
            )
            adapter(None, _spec(), "gemini", "gemini-3.7-flash", lambda: None)
            self.assertEqual(len(snapshots[-1]), 1)

        with legacy.vision_provider_circuit_scope():
            adapter(None, _spec(), "gemini", "gemini-3.7-flash", lambda: None)
            self.assertEqual(snapshots[-1], [])

    def test_existing_wider_taskspec_is_never_narrowed(self) -> None:
        seen = []

        def fake_route(ledger, spec, provider, resolved_model, fn, *args, **kwargs):
            seen.append(spec.max_provider_attempts)
            return {"status": "pass"}

        adapter = self._adapter_around(fake_route)
        with legacy.vision_provider_circuit_scope():
            adapter(None, _spec(max_attempts=5), "gemini", "gemini-3.7-flash", lambda: None)
        self.assertEqual(seen, [5])

    def test_stale_planning_429_before_current_run_baseline_is_ignored(self) -> None:
        old = {
            "provider": "gemini",
            "result": "429",
            "error_detail": "old run quota",
        }
        current = {
            "provider": "groq",
            "result": "success",
            "error_detail": None,
        }
        token = transport._RUN181_TELEMETRY_BASELINE.set((1, 0))
        try:
            with mock.patch.object(
                planner_router,
                "get_telemetry",
                return_value=[old, current],
            ), mock.patch.object(text_mesh, "_AUDIT_ROUTE_TELEMETRY", []):
                transport._scoped_refresh_runtime_provider_health()
        finally:
            transport._RUN181_TELEMETRY_BASELINE.reset(token)
        self.assertIsNone(
            health.provider_unavailable(
                "gemini",
                model=transport._runtime_gemini_generation_model(),
                quota_domain=run181.GEMINI_GENERATION_QUOTA_DOMAIN,
            )
        )

    def test_current_run_planning_429_after_baseline_is_imported(self) -> None:
        old = {
            "provider": "gemini",
            "result": "success",
            "error_detail": None,
        }
        current = {
            "provider": "gemini",
            "result": "429",
            "error_detail": "current run quota",
        }
        token = transport._RUN181_TELEMETRY_BASELINE.set((1, 0))
        try:
            with mock.patch.object(
                planner_router,
                "get_telemetry",
                return_value=[old, current],
            ), mock.patch.object(text_mesh, "_AUDIT_ROUTE_TELEMETRY", []):
                transport._scoped_refresh_runtime_provider_health()
        finally:
            transport._RUN181_TELEMETRY_BASELINE.reset(token)
        evidence = health.provider_unavailable(
            "gemini",
            model=transport._runtime_gemini_generation_model(),
            quota_domain=run181.GEMINI_GENERATION_QUOTA_DOMAIN,
        )
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence.source, "planning_telemetry")
        self.assertIn("current run quota", evidence.reason)

    def test_runtime_alias_is_canonicalized_when_publishing_planning_429(self) -> None:
        current = {
            "provider": "gemini",
            "result": "429",
            "error_detail": "alias quota",
        }
        token = transport._RUN181_TELEMETRY_BASELINE.set((0, 0))
        try:
            with mock.patch.object(
                planner_router,
                "get_telemetry",
                return_value=[current],
            ), mock.patch.object(text_mesh, "_AUDIT_ROUTE_TELEMETRY", []), mock.patch.object(
                run181,
                "_gemini_runtime_model",
                return_value="gemini-2.5-flash",
            ):
                transport._scoped_refresh_runtime_provider_health()
        finally:
            transport._RUN181_TELEMETRY_BASELINE.reset(token)
        self.assertIsNotNone(
            health.provider_unavailable(
                "gemini",
                model="gemini-3.7-flash",
                quota_domain=run181.GEMINI_GENERATION_QUOTA_DOMAIN,
            )
        )
        self.assertIsNone(
            health.provider_unavailable(
                "gemini",
                model="gemini-2.5-flash",
                quota_domain=run181.GEMINI_GENERATION_QUOTA_DOMAIN,
            )
        )

    def test_stale_text_audit_429_before_current_run_baseline_is_ignored(self) -> None:
        old_route = {
            "attempts": [
                {
                    "provider": "gemini",
                    "outcome": "rate_limited",
                    "detail": "old audit quota",
                }
            ]
        }
        current_route = {
            "attempts": [
                {
                    "provider": "groq:qwen/qwen3.8-27b",
                    "outcome": "success",
                    "detail": None,
                }
            ]
        }
        token = transport._RUN181_TELEMETRY_BASELINE.set((0, 1))
        try:
            with mock.patch.object(planner_router, "get_telemetry", return_value=[]), mock.patch.object(
                text_mesh,
                "_AUDIT_ROUTE_TELEMETRY",
                [old_route, current_route],
            ):
                transport._scoped_refresh_runtime_provider_health()
        finally:
            transport._RUN181_TELEMETRY_BASELINE.reset(token)
        self.assertIsNone(
            health.provider_unavailable(
                "gemini",
                model=transport._runtime_gemini_generation_model(),
                quota_domain=run181.GEMINI_GENERATION_QUOTA_DOMAIN,
            )
        )


if __name__ == "__main__":
    unittest.main()

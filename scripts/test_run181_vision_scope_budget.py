from __future__ import annotations

import unittest
from unittest import mock

from isco_video_agent.ai_budget import Capability, Priority, TaskSpec
from scripts import provider_health_registry as health
from scripts import run181_vision_mesh_closure as run181
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


if __name__ == "__main__":
    unittest.main()

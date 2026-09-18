from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import isco_video_agent.resilient_planner as staged
from isco_video_agent.ai_budget import (
    BudgetLedger,
    Capability,
    Priority,
    TaskSpec,
    budget_task_scope,
)

from scripts import checkpoint_namespace_guard as checkpoint_guard
from scripts import planning_stage_contract as planning
from scripts import task_level_planner_router as planner_router
from scripts import text_audit_provider_mesh as mesh


_FACT_PASS = {
    "status": "pass",
    "unsupported_claims": [],
    "professional_advice_flags": [],
    "expert_persona_flags": [],
    "notes": [],
}


def _spec() -> TaskSpec:
    return TaskSpec(
        task_id="FACTUALITY_AUDIT",
        kind="FACTUALITY_AUDIT",
        priority=Priority.P0,
        capability=Capability.TEXT,
        max_provider_attempts=3,
        schema_repair_allowed=False,
        local_fallback=False,
        semantic_block_is_final=True,
    )


class Run269PlanningToTextAuditCooldownHandoffTests(unittest.TestCase):
    """Exact Run #269 boundary: Planning Retry-After must survive into Text Audit admission."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cache_path = Path(self.tmp.name) / "planning-checkpoint.json"
        self.key_path = Path(self.tmp.name) / "gemini-key"
        self.key_path.write_text("fake-key", encoding="utf-8")
        self.env = mock.patch.dict(
            os.environ,
            {"GEMINI_API_KEY_FILE": str(self.key_path)},
            clear=False,
        )
        self.env.start()
        self.cache = mock.patch.object(planner_router, "CACHE_PATH", self.cache_path)
        self.cache.start()
        self.old_json_text = staged.json_text
        self.old_schema_adapter = planner_router._structured_schema_for_prompt
        staged.json_text = lambda *_args, **_kwargs: {}
        planner_router._TELEMETRY.clear()
        mesh._AUDIT_ROUTE_TELEMETRY.clear()

        self.clock = {"mono": 1000.0, "epoch": 1_800_000_000.0}
        self.mono = mock.patch.object(planning.time, "monotonic", side_effect=lambda: self.clock["mono"])
        self.wall = mock.patch.object(planning.time, "time", side_effect=lambda: self.clock["epoch"])
        self.sleep = mock.patch.object(planning.time, "sleep", return_value=None)
        self.mono.start()
        self.wall.start()
        self.sleep.start()

        checkpoint_guard.install_checkpoint_namespace_guard()
        checkpoint_guard.assert_stage_checkpoint_namespace_guarded()
        planning.install_planning_contract_router()

    def tearDown(self) -> None:
        staged.json_text = self.old_json_text
        planner_router._structured_schema_for_prompt = self.old_schema_adapter
        self.sleep.stop()
        self.wall.stop()
        self.mono.stop()
        self.cache.stop()
        self.env.stop()
        self.tmp.cleanup()

    def _planning_short_window_then_groq(self, retry_after: float = 58.5) -> None:
        valid = {
            "sections": [
                {"id": "s1", "narration": "نص صالح", "key_point": "key-s1"}
            ]
        }
        gemini_calls = {"n": 0}
        groq_calls = {"n": 0}

        def gemini_short_window(*_args, **_kwargs):
            gemini_calls["n"] += 1
            raise RuntimeError(
                f"GEMINI_HTTP_429 status=429 quota exceeded "
                f"requests per minute Retry-After={retry_after}"
            )

        def groq_success(_prompt):
            groq_calls["n"] += 1
            return valid

        base = planning.script_stage_spec("full_script", ["s1"])
        spec = planning.PlanningStageSpec(
            stage_id=base.stage_id,
            contract_id=base.contract_id,
            output_schema=base.output_schema,
            semantic_rules=base.semantic_rules,
            provider_policy=planning.ProviderPolicy(
                providers=("gemini", "groq"),
                max_attempts_per_provider=1,
                max_total_attempts=2,
                completion_tokens=base.provider_policy.completion_tokens,
                max_prompt_utf8_bytes=(),
            ),
            cache_policy=base.cache_policy,
        )

        with mock.patch.object(planner_router, "gemini_json_text", side_effect=gemini_short_window), \
                mock.patch.object(planner_router, "_groq_call", side_effect=groq_success), \
                planning.request_stage_scope(spec):
            result = staged.json_text("request-key", "run-269 planning boundary")

        self.assertEqual(result, valid)
        self.assertEqual(gemini_calls["n"], 1)
        self.assertEqual(groq_calls["n"], 1)

    def _audit_route(self, gemini_call, *, cooldown=None, mistral_call=None):
        mistral_call = mistral_call or (lambda _p: dict(_FACT_PASS))
        with mock.patch.object(mesh, "_groq_route_models", return_value=[]), \
                mock.patch.object(mesh.run125, "openrouter_preflight_blocked", return_value=True), \
                mock.patch.object(mesh.planner_router, "_mistral_route_ready", return_value=True), \
                mock.patch.object(mesh, "_mistral_audit_json", side_effect=mistral_call):
            return mesh._mesh_route(
                [
                    ("gemini", gemini_call),
                    ("openrouter", lambda _p: dict(_FACT_PASS)),
                ],
                "run-269 factuality prompt",
                cooldown=cooldown,
            )

    def test_exact_run269_active_retry_after_prevents_premature_gemini_wire(self) -> None:
        # T0: Gemini 429 with Retry-After=58.5s. Planning then recovers via Groq.
        self._planning_short_window_then_groq(58.5)

        # T1/T2: Text Audit begins 33s later, leaving ~25.5s on the provider deadline.
        self.clock["mono"] += 33.0
        self.clock["epoch"] += 33.0
        audit_gemini_calls = {"n": 0}
        mistral_calls = {"n": 0}

        def audit_gemini(_prompt):
            audit_gemini_calls["n"] += 1
            return dict(_FACT_PASS)

        def audit_mistral(_prompt):
            mistral_calls["n"] += 1
            return dict(_FACT_PASS)

        result = self._audit_route(audit_gemini, mistral_call=audit_mistral)

        self.assertFalse(result.exhausted)
        self.assertEqual(audit_gemini_calls["n"], 0, "Gemini wire occurred before inherited Retry-After expiry")
        self.assertEqual(mistral_calls["n"], 1)
        self.assertEqual(result.provider, "mistral")

    def test_provider_is_restored_after_inherited_deadline_expires(self) -> None:
        self._planning_short_window_then_groq(58.5)
        self.clock["mono"] += 59.0
        self.clock["epoch"] += 59.0
        calls = {"gemini": 0}

        def audit_gemini(_prompt):
            calls["gemini"] += 1
            return dict(_FACT_PASS)

        result = self._audit_route(audit_gemini)

        self.assertEqual(calls["gemini"], 1)
        self.assertEqual(result.provider, "gemini")

    def test_no_planning_cooldown_keeps_text_audit_behavior_unchanged(self) -> None:
        calls = {"gemini": 0}

        def audit_gemini(_prompt):
            calls["gemini"] += 1
            return dict(_FACT_PASS)

        result = self._audit_route(audit_gemini)
        self.assertEqual(calls["gemini"], 1)
        self.assertEqual(result.provider, "gemini")

    def test_daily_quota_is_not_published_as_short_window_handoff(self) -> None:
        valid = {"sections": [{"id": "s1", "narration": "نص", "key_point": "key"}]}

        def gemini_daily(*_args, **_kwargs):
            raise RuntimeError("GEMINI_HTTP_429 status=429 quota exceeded requests per day")

        base = planning.script_stage_spec("full_script", ["s1"])
        spec = planning.PlanningStageSpec(
            stage_id=base.stage_id,
            contract_id=base.contract_id,
            output_schema=base.output_schema,
            semantic_rules=base.semantic_rules,
            provider_policy=planning.ProviderPolicy(
                providers=("gemini", "groq"),
                max_attempts_per_provider=1,
                max_total_attempts=2,
                completion_tokens=base.provider_policy.completion_tokens,
                max_prompt_utf8_bytes=(),
            ),
            cache_policy=base.cache_policy,
        )
        with mock.patch.object(planner_router, "gemini_json_text", side_effect=gemini_daily), \
                mock.patch.object(planner_router, "_groq_call", return_value=valid), \
                planning.request_stage_scope(spec):
            staged.json_text("request-key", "daily quota case")

        calls = {"gemini": 0}

        def audit_gemini(_prompt):
            calls["gemini"] += 1
            return dict(_FACT_PASS)

        result = self._audit_route(audit_gemini)
        self.assertEqual(calls["gemini"], 1)
        self.assertEqual(result.provider, "gemini")

    def test_gemini_handoff_does_not_block_other_provider_or_groq_model_routes(self) -> None:
        self._planning_short_window_then_groq(58.5)
        self.clock["mono"] += 33.0
        self.clock["epoch"] += 33.0
        captured = {}

        def fake_engine_route(providers, prompt, *, cooldown=None):
            captured["names"] = [name for name, _call in providers]
            return mock.Mock(provider="groq:openai/gpt-oss-120b", exhausted=False, attempts=[])

        with mock.patch.object(
            mesh, "_active_groq_pool_tail", return_value=("openai/gpt-oss-120b",)
        ), mock.patch.object(
            mesh, "_groq_model_route_eligible", return_value=True
        ), mock.patch.object(
            mesh.run125, "openrouter_preflight_blocked", return_value=True
        ), mock.patch.object(
            mesh.planner_router, "_mistral_route_ready", return_value=True
        ), mock.patch.object(
            mesh.engine_audit_router, "route_text_audit", side_effect=fake_engine_route
        ):
            mesh._mesh_route(
                [("gemini", lambda _p: dict(_FACT_PASS)), ("openrouter", lambda _p: dict(_FACT_PASS))],
                "audit-prompt",
            )

        self.assertNotIn("gemini", captured["names"])
        self.assertIn("groq:openai/gpt-oss-120b", captured["names"])
        self.assertIn("mistral", captured["names"])

    def test_native_text_audit_429_circuit_semantics_are_unchanged(self) -> None:
        cooldown = set()
        calls = {"gemini": 0, "mistral": 0}

        def gemini_429(_prompt):
            calls["gemini"] += 1
            raise RuntimeError("429 rate limit inside Text Audit")

        def mistral_pass(_prompt):
            calls["mistral"] += 1
            return dict(_FACT_PASS)

        result = self._audit_route(gemini_429, cooldown=cooldown, mistral_call=mistral_pass)

        self.assertEqual(result.provider, "mistral")
        self.assertEqual(calls, {"gemini": 1, "mistral": 1})
        self.assertIn("gemini", cooldown)

    def test_inherited_skip_consumes_no_provider_attempt_and_adds_no_retry(self) -> None:
        self._planning_short_window_then_groq(58.5)
        self.clock["mono"] += 33.0
        self.clock["epoch"] += 33.0

        ledger = BudgetLedger("film", enforce=True)
        calls = {"gemini": 0, "mistral": 0}

        def forbidden_gemini(_prompt):
            calls["gemini"] += 1
            raise AssertionError("premature Gemini wire")

        def mistral_pass(_prompt):
            calls["mistral"] += 1
            return dict(_FACT_PASS)

        with budget_task_scope(
            ledger,
            _spec(),
            requested_model="gemini-3.7-flash",
        ):
            result = self._audit_route(
                forbidden_gemini,
                mistral_call=mistral_pass,
            )

        self.assertEqual(result.provider, "mistral")
        self.assertEqual(calls, {"gemini": 0, "mistral": 1})
        summary = ledger.to_summary()
        self.assertEqual(summary["provider_attempts"]["total"], 1)
        self.assertEqual(summary["provider_attempts"]["by_provider"], {"mistral": 1})

    def test_clock_domain_uses_remaining_monotonic_time_not_wall_clock_epoch(self) -> None:
        self._planning_short_window_then_groq(58.5)
        # Deliberately make wall time unrelated to the monotonic delta.
        self.clock["mono"] += 33.0
        self.clock["epoch"] += 9_999_999.0
        calls = {"gemini": 0}

        def audit_gemini(_prompt):
            calls["gemini"] += 1
            return dict(_FACT_PASS)

        self._audit_route(audit_gemini)
        self.assertEqual(calls["gemini"], 0)


if __name__ == "__main__":
    unittest.main()

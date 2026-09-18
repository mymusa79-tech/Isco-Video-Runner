from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from dataclasses import replace
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


class Run269TextAuditCooldownHandoffTests(unittest.TestCase):
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
        planner_router._USED_PROVIDERS.clear()
        planner_router._TELEMETRY.clear()
        mesh._AUDIT_ROUTE_TELEMETRY.clear()

        self.clock = {"monotonic": 1000.0}
        self.monotonic = mock.patch.object(
            planning.time, "monotonic", side_effect=lambda: self.clock["monotonic"]
        )
        self.monotonic.start()
        self.sleep = mock.patch.object(planning.time, "sleep", return_value=None)
        self.sleep.start()

        checkpoint_guard.install_checkpoint_namespace_guard()
        checkpoint_guard.assert_stage_checkpoint_namespace_guarded()
        planning.install_planning_contract_router()

    def tearDown(self) -> None:
        staged.json_text = self.old_json_text
        planner_router._structured_schema_for_prompt = self.old_schema_adapter
        self.sleep.stop()
        self.monotonic.stop()
        self.cache.stop()
        self.env.stop()
        self.tmp.cleanup()

    def _append_spec(self):
        base = planning.append_stage_spec(
            ["s1"],
            required_floor_words=10,
            minimum_append_words=20,
            maximum_append_words=40,
        )
        return replace(
            base,
            provider_policy=replace(
                base.provider_policy,
                providers=("gemini", "groq"),
                max_attempts_per_provider=1,
                max_total_attempts=2,
                second_pass_after_full_exhaustion=False,
            ),
        )

    def _arm_run269_short_window(self, retry_after: float = 58.5) -> None:
        valid = {
            "additions": [
                {
                    "id": "s1",
                    "append_text": "إضافة صالحة تحافظ على نفس الفكرة",
                }
            ]
        }

        def gemini_short_window(*_args, **_kwargs):
            planner_router._last_call_rate_limit_headers["retry_after"] = str(retry_after)
            raise RuntimeError(
                f"GEMINI_HTTP_429 status=429 quota exceeded requests per minute "
                f"Retry-After={retry_after}"
            )

        with mock.patch.object(
            planner_router, "gemini_json_text", side_effect=gemini_short_window
        ), mock.patch.object(
            planner_router, "_groq_call", return_value=valid
        ), planning.request_stage_scope(self._append_spec()):
            result = staged.json_text("request-key", "run-269 append request")

        self.assertEqual(result, valid)

    def _mesh_call(self, gemini_call, openrouter_call, *, cooldown=None):
        with mock.patch.object(
            mesh.planner_router, "_mistral_route_ready", return_value=False
        ), mock.patch.object(
            mesh, "_groq_model_route_eligible", return_value=False
        ), mock.patch.object(
            mesh.run125, "openrouter_preflight_blocked", return_value=False
        ):
            return mesh._mesh_route(
                [("gemini", gemini_call), ("openrouter", openrouter_call)],
                "text-audit-prompt",
                cooldown=cooldown,
            )

    def test_exact_run269_timeline_blocks_premature_wire_then_restores_gemini(self) -> None:
        self._arm_run269_short_window(58.5)
        initial = planning.planning_provider_cooldown_evidence("gemini")
        self.assertIsNotNone(initial)
        self.assertAlmostEqual(initial["remaining_seconds"], 58.5, delta=0.01)
        self.assertEqual(initial["reason"], "short_window_retry_after")
        self.assertEqual(initial["scope"], "provider")

        # T1/T2: Planning completes through Groq about 33s after the Gemini 429,
        # leaving about 25.5s of the provider-owned short window.
        self.clock["monotonic"] += 33.0
        active = planning.planning_provider_cooldown_evidence("gemini")
        self.assertIsNotNone(active)
        self.assertAlmostEqual(active["remaining_seconds"], 25.5, delta=0.01)

        # Pre-fix control: Engine routing alone knows nothing about Planning cooldown
        # evidence, so it wires Gemini immediately while the deadline is still active.
        legacy_wire_times = []

        def legacy_gemini(_prompt):
            legacy_wire_times.append(self.clock["monotonic"])
            return dict(_FACT_PASS)

        legacy = mesh.engine_audit_router.route_text_audit(
            [("gemini", legacy_gemini)],
            "text-audit-prompt",
        )
        self.assertEqual(legacy.provider, "gemini")
        self.assertEqual(legacy_wire_times, [1033.0])
        self.assertLess(legacy_wire_times[0], 1058.5)

        fixed_gemini_wires = []
        openrouter_wires = []

        def fixed_gemini(_prompt):
            fixed_gemini_wires.append(self.clock["monotonic"])
            return dict(_FACT_PASS)

        def openrouter(_prompt):
            openrouter_wires.append(self.clock["monotonic"])
            return dict(_FACT_PASS)

        text_audit_circuit = set()
        capture = io.StringIO()
        with contextlib.redirect_stdout(capture):
            before_expiry = self._mesh_call(
                fixed_gemini,
                openrouter,
                cooldown=text_audit_circuit,
            )

        self.assertEqual(before_expiry.provider, "openrouter")
        self.assertEqual(fixed_gemini_wires, [])
        self.assertEqual(openrouter_wires, [1033.0])
        self.assertNotIn("gemini", text_audit_circuit)
        self.assertEqual(before_expiry.attempts[0].provider, "gemini")
        self.assertEqual(before_expiry.attempts[0].outcome.value, "other")
        self.assertIn("INHERITED_PROVIDER_COOLDOWN", before_expiry.attempts[0].detail)
        self.assertIn("inherited_provider_cooldown=true", capture.getvalue())
        self.assertIn("remaining_seconds=25.50", capture.getvalue())
        self.assertIn("action=skip_without_wire", capture.getvalue())

        # After the exact inherited window expires, the same Text Audit circuit does
        # not retain a permanent block. Gemini is eligible and wins in normal order.
        self.clock["monotonic"] += 26.0
        capture = io.StringIO()
        with contextlib.redirect_stdout(capture):
            after_expiry = self._mesh_call(
                fixed_gemini,
                openrouter,
                cooldown=text_audit_circuit,
            )

        self.assertEqual(after_expiry.provider, "gemini")
        self.assertEqual(fixed_gemini_wires, [1059.0])
        self.assertNotIn("gemini", text_audit_circuit)
        self.assertIn("inherited_provider_cooldown_expired=true", capture.getvalue())

    def test_no_planning_cooldown_keeps_text_audit_behavior_unchanged(self) -> None:
        calls = []
        with mock.patch.object(
            planning, "planning_provider_cooldown_evidence", return_value=None
        ):
            result = self._mesh_call(
                lambda _p: calls.append("gemini") or dict(_FACT_PASS),
                lambda _p: calls.append("openrouter") or dict(_FACT_PASS),
            )
        self.assertEqual(result.provider, "gemini")
        self.assertEqual(calls, ["gemini"])

    def test_handoff_exports_remaining_time_only_and_ignores_wall_clock(self) -> None:
        self._arm_run269_short_window(58.5)
        with mock.patch.object(planning.time, "time", return_value=9_999_999_999.0):
            evidence = planning.planning_provider_cooldown_evidence("gemini")

        self.assertIsNotNone(evidence)
        self.assertAlmostEqual(evidence["remaining_seconds"], 58.5, delta=0.01)
        self.assertNotIn("deadline_monotonic", evidence)
        self.assertNotIn("deadline_epoch", evidence)

    def test_daily_quota_is_not_exported_as_short_window_handoff(self) -> None:
        valid = {
            "additions": [
                {"id": "s1", "append_text": "إضافة صالحة تحافظ على نفس الفكرة"}
            ]
        }

        def gemini_daily(*_args, **_kwargs):
            raise RuntimeError(
                "GEMINI_HTTP_429 status=429 quota exceeded requests per day"
            )

        with mock.patch.object(
            planner_router, "gemini_json_text", side_effect=gemini_daily
        ), mock.patch.object(
            planner_router, "_groq_call", return_value=valid
        ), planning.request_stage_scope(self._append_spec()):
            staged.json_text("request-key", "daily-quota control")

        self.assertIsNone(planning.planning_provider_cooldown_evidence("gemini"))

    def test_gemini_handoff_does_not_affect_other_provider(self) -> None:
        calls = []
        evidence = {
            "provider": "gemini",
            "model": None,
            "scope": "provider",
            "source": "planning",
            "reason": "short_window_retry_after",
            "remaining_seconds": 25.0,
            "expired": False,
        }

        def evidence_for(provider, **_kwargs):
            return evidence if provider == "gemini" else None

        with mock.patch.object(
            planning, "planning_provider_cooldown_evidence", side_effect=evidence_for
        ):
            guarded = mesh._planning_cooldown_guard(
                "mistral", lambda _p: calls.append("mistral") or dict(_FACT_PASS)
            )
            result = guarded("prompt")

        self.assertEqual(result["status"], "pass")
        self.assertEqual(calls, ["mistral"])

    def test_groq_model_routes_remain_owned_by_existing_model_specific_capacity_logic(self) -> None:
        calls = []
        with mock.patch.object(
            planning,
            "planning_provider_cooldown_evidence",
            side_effect=AssertionError("Groq handoff must not be consulted"),
        ):
            first = mesh._planning_cooldown_guard(
                "groq:openai/gpt-oss-120b",
                lambda _p: calls.append("120b") or dict(_FACT_PASS),
            )
            second = mesh._planning_cooldown_guard(
                "groq:qwen/qwen3.8-27b",
                lambda _p: calls.append("qwen") or dict(_FACT_PASS),
            )
            self.assertEqual(first("p1")["status"], "pass")
            self.assertEqual(second("p2")["status"], "pass")

        self.assertEqual(calls, ["120b", "qwen"])

    def test_native_text_audit_429_still_opens_existing_circuit(self) -> None:
        gemini_calls = []
        openrouter_calls = []
        cooldown = set()

        def gemini(_prompt):
            gemini_calls.append("wire")
            raise RuntimeError("429 quota exceeded")

        def openrouter(_prompt):
            openrouter_calls.append("wire")
            return dict(_FACT_PASS)

        with mock.patch.object(
            planning, "planning_provider_cooldown_evidence", return_value=None
        ):
            first = self._mesh_call(gemini, openrouter, cooldown=cooldown)
            second = self._mesh_call(gemini, openrouter, cooldown=cooldown)

        self.assertEqual(first.provider, "openrouter")
        self.assertEqual(second.provider, "openrouter")
        self.assertEqual(gemini_calls, ["wire"])
        self.assertEqual(openrouter_calls, ["wire", "wire"])
        self.assertIn("gemini", cooldown)
        self.assertEqual(second.attempts[0].outcome.value, "circuit_open")

    def test_inherited_skip_consumes_zero_attempts_and_adds_no_retry(self) -> None:
        evidence = {
            "provider": "gemini",
            "model": None,
            "scope": "provider",
            "source": "planning",
            "reason": "short_window_retry_after",
            "remaining_seconds": 25.0,
            "expired": False,
        }
        ledger = BudgetLedger("film", enforce=True)
        spec = TaskSpec(
            task_id="FACTUALITY_AUDIT",
            kind="FACTUALITY_AUDIT",
            priority=Priority.P0,
            capability=Capability.TEXT,
            max_provider_attempts=1,
            schema_repair_allowed=False,
            local_fallback=False,
            semantic_block_is_final=True,
        )
        calls = []

        def evidence_for(provider, **_kwargs):
            return evidence if provider == "gemini" else None

        with mock.patch.object(
            planning, "planning_provider_cooldown_evidence", side_effect=evidence_for
        ), budget_task_scope(
            ledger, spec, requested_model="gemini-3.7-flash"
        ):
            result = self._mesh_call(
                lambda _p: calls.append("gemini") or dict(_FACT_PASS),
                lambda _p: calls.append("openrouter") or dict(_FACT_PASS),
            )

        self.assertEqual(result.provider, "openrouter")
        self.assertEqual(calls, ["openrouter"])
        summary = ledger.to_summary()["provider_attempts"]
        self.assertEqual(summary["total"], 1)
        self.assertEqual(summary["by_provider"], {"openrouter": 1})
        self.assertEqual(len(result.attempts), 2)
        self.assertEqual(result.attempts[0].outcome.value, "other")
        self.assertEqual(result.attempts[1].outcome.value, "success")


if __name__ == "__main__":
    unittest.main()

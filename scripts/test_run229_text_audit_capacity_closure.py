from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest import mock

import isco_video_agent.text_audit_router as engine_audit_router

from scripts import provider_capacity_hardening as capacity
from scripts import text_audit_capacity_ownership as ownership
from scripts import text_audit_provider_mesh as mesh


class Run229TextAuditCapacityClosureRegressionTests(unittest.TestCase):
    def setUp(self):
        ownership._AUDIT_WAITED_TASK_IDS.clear()

    def test_run229_waits_on_120b_when_only_later_qwen_route_is_circuit_open(self):
        """Reproduce Run #229: 3839 remain, 5112 required, reset in 30.39s."""
        now = 1000.0
        current_model = "openai/gpt-oss-120b"
        later_model = "qwen/qwen3.8-27b"
        current_decision = {
            "action": "wait",
            "remaining_tokens": 3839,
            "actual_limit": 8000,
        }

        def admission(model: str, required: int):
            self.assertEqual(required, 5112)
            if model == later_model:
                self.fail("circuit-open Qwen must not participate in capacity look-ahead")
            self.assertEqual(model, current_model)
            return current_decision

        cooldown_token = engine_audit_router._RUN_COOLDOWN.set(
            {mesh._groq_route_label(later_model)}
        )
        try:
            with mock.patch.object(
                capacity,
                "groq_admission_decision",
                side_effect=admission,
            ), mock.patch.object(
                capacity,
                "_model_state",
                side_effect=lambda model: {"reset_at_epoch": now + 30.39},
            ), mock.patch.object(
                capacity.time,
                "time",
                return_value=now,
            ), mock.patch.object(
                capacity.time,
                "sleep",
            ) as sleep, mock.patch.object(
                ownership.run125,
                "openrouter_preflight_blocked",
                return_value=True,
            ), mock.patch.object(
                mesh,
                "_active_groq_pool_tail",
                return_value=(current_model, later_model),
            ):
                waited = ownership._audit_wait_pacing(
                    {"estimated_request_tokens": 5112},
                    model_name=current_model,
                )
        finally:
            engine_audit_router._RUN_COOLDOWN.reset(cooldown_token)

        self.assertAlmostEqual(waited, 31.89, places=2)
        sleep.assert_called_once()
        self.assertAlmostEqual(sleep.call_args.args[0], 31.89, places=2)

    def test_canonical_mesh_removes_generic_groq_alias(self):
        sentinel = lambda _prompt: {"status": "pass"}
        providers = [
            ("gemini", sentinel),
            ("groq", sentinel),
            ("openrouter", sentinel),
        ]

        normalized = ownership._canonical_audit_providers(providers)

        self.assertEqual([name for name, _call in normalized], ["gemini", "openrouter"])

    def test_non_mesh_provider_list_preserves_generic_groq_alias(self):
        sentinel = lambda _prompt: {"status": "pass"}
        providers = [("gemini", sentinel), ("groq", sentinel)]

        normalized = ownership._canonical_audit_providers(providers)

        self.assertEqual([name for name, _call in normalized], ["gemini", "groq"])

    def test_canonical_route_contains_only_model_scoped_groq_candidates(self):
        sentinel = lambda _prompt: {"status": "pass"}
        providers = [
            ("gemini", sentinel),
            ("groq", sentinel),
            ("openrouter", sentinel),
        ]
        captured: list[str] = []

        def fake_route(routed, _prompt, *, cooldown=None):
            del cooldown
            captured.extend(name for name, _call in routed)
            return SimpleNamespace(provider=None, exhausted=True, attempts=[])

        with mock.patch.object(
            ownership.run125,
            "openrouter_preflight_blocked",
            return_value=True,
        ), mock.patch.object(
            mesh,
            "_groq_route_models",
            return_value=["openai/gpt-oss-120b", "qwen/qwen3.8-27b"],
        ), mock.patch.object(
            mesh.engine_audit_router,
            "route_text_audit",
            side_effect=fake_route,
        ):
            ownership._canonical_audit_route(providers, "audit prompt")

        self.assertEqual(
            captured,
            [
                "gemini",
                "groq:openai/gpt-oss-120b",
                "groq:qwen/qwen3.8-27b",
            ],
        )
        self.assertNotIn("groq", captured)
        self.assertNotIn("groq:openai/gpt-oss-20b", captured)

    def test_long_or_untrusted_reset_remains_failover_without_sleep(self):
        decision = {
            "action": "wait",
            "remaining_tokens": 3839,
            "actual_limit": 8000,
        }
        for state in ({}, {"reset_at_epoch": 1061.0}):
            with self.subTest(state=state), mock.patch.object(
                capacity,
                "groq_admission_decision",
                return_value=decision,
            ), mock.patch.object(
                capacity,
                "_model_state",
                return_value=state,
            ), mock.patch.object(
                capacity.time,
                "time",
                return_value=1000.0,
            ), mock.patch.object(
                capacity.time,
                "sleep",
            ) as sleep:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "GROQ_TPM_WINDOW_BUSY_PRECHECK.*action=failover_without_http",
                ):
                    ownership._audit_wait_pacing(
                        {"estimated_request_tokens": 5112},
                        model_name="openai/gpt-oss-120b",
                    )
                sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()

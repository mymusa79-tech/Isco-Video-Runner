from __future__ import annotations

import unittest
from unittest import mock

from scripts import text_audit_provider_mesh as mesh


_CONTENT_PASS = {
    "status": "pass",
    "duplicate_groups": [],
    "reason": "",
}


class Run157TextAuditPoolIsolationRegressionTests(unittest.TestCase):
    """Regression for the production install order observed in Run #157."""

    def setUp(self):
        mesh._AUDIT_ROUTE_TELEMETRY.clear()

    @staticmethod
    def _run157_capacity(_prompt: str, model_name: str) -> tuple[dict, dict]:
        if model_name == "openai/gpt-oss-120b":
            return (
                {"estimated_request_tokens": 2731},
                {
                    "action": "wait",
                    "actual_limit": 8000,
                    "remaining_tokens": 934,
                },
            )
        if model_name == "qwen/qwen3.8-27b":
            return (
                {"estimated_request_tokens": 2731},
                {
                    "action": "admit",
                    "actual_limit": 8000,
                    "remaining_tokens": None,
                },
            )
        return (
            {"estimated_request_tokens": 2731},
            {"action": "admit", "actual_limit": 8000},
        )

    def test_run125_planning_pool_narrowing_does_not_narrow_audit_pool(self):
        # install_run125_cache_prefix_contract() intentionally leaves Planning with
        # these two models only. Run #157 had already advanced the active cursor to
        # 120b when Text Audit Mesh was installed.
        with mock.patch.object(
            mesh.run125,
            "_GROQ_MODEL_POOL",
            ("openai/gpt-oss-20b", "openai/gpt-oss-120b"),
        ), mock.patch.object(
            mesh.run125,
            "_active_groq_model",
            return_value="openai/gpt-oss-120b",
        ):
            self.assertEqual(
                mesh._active_groq_pool_tail(),
                ("openai/gpt-oss-120b", "qwen/qwen3.8-27b"),
            )
            self.assertEqual(
                mesh.run125._GROQ_MODEL_POOL,
                ("openai/gpt-oss-20b", "openai/gpt-oss-120b"),
            )

    def test_run157_capacity_state_selects_qwen_without_mocking_eligibility(self):
        # Production evidence: 120b had 934 TPM remaining and a trustworthy ~53s
        # reset. Qwen was healthy, but the old shared mutable pool removed it before
        # eligibility was evaluated. Keep the real eligibility function in this test.
        with mock.patch.object(
            mesh.run125,
            "_GROQ_MODEL_POOL",
            ("openai/gpt-oss-20b", "openai/gpt-oss-120b"),
        ), mock.patch.object(
            mesh.run125,
            "_active_groq_model",
            return_value="openai/gpt-oss-120b",
        ), mock.patch.object(
            mesh,
            "_groq_secret_available",
            return_value=True,
        ), mock.patch.object(
            mesh,
            "_groq_request_capacity",
            side_effect=self._run157_capacity,
        ), mock.patch.object(
            mesh.capacity,
            "_model_state",
            side_effect=lambda model: (
                {"reset_at_epoch": 9999999999.0}
                if model == "openai/gpt-oss-120b"
                else {}
            ),
        ):
            selected = mesh._groq_route_models(
                "audit-prompt",
                openrouter_blocked=True,
            )

        self.assertEqual(
            selected,
            ["openai/gpt-oss-120b", "qwen/qwen3.8-27b"],
        )

    def test_run157_120b_busy_falls_through_to_qwen_with_same_three_attempt_budget(self):
        calls: list[str] = []

        def gemini(_prompt: str) -> dict:
            raise RuntimeError("429 quota exceeded")

        def groq(_prompt: str, *, model_name: str) -> dict:
            calls.append(model_name)
            if model_name == "openai/gpt-oss-120b":
                raise RuntimeError(
                    "GROQ_TPM_WINDOW_BUSY_PRECHECK "
                    "model=openai/gpt-oss-120b required_estimate=2731 "
                    "remaining=934 reset_in=52.99s"
                )
            return dict(_CONTENT_PASS)

        with mock.patch.object(
            mesh.run125,
            "_GROQ_MODEL_POOL",
            ("openai/gpt-oss-20b", "openai/gpt-oss-120b"),
        ), mock.patch.object(
            mesh.run125,
            "_active_groq_model",
            return_value="openai/gpt-oss-120b",
        ), mock.patch.object(
            mesh,
            "_groq_secret_available",
            return_value=True,
        ), mock.patch.object(
            mesh,
            "_groq_request_capacity",
            side_effect=self._run157_capacity,
        ), mock.patch.object(
            mesh.capacity,
            "_model_state",
            side_effect=lambda model: (
                {"reset_at_epoch": 9999999999.0}
                if model == "openai/gpt-oss-120b"
                else {}
            ),
        ), mock.patch.object(
            mesh,
            "_groq_audit_json",
            side_effect=groq,
        ), mock.patch.object(
            mesh.run125,
            "openrouter_preflight_blocked",
            return_value=True,
        ):
            result = mesh._mesh_route(
                [
                    ("gemini", gemini),
                    ("openrouter", lambda _p: dict(_CONTENT_PASS)),
                ],
                "audit-prompt",
            )

        self.assertFalse(result.exhausted)
        self.assertEqual(result.provider, "groq:qwen/qwen3.8-27b")
        self.assertEqual(
            calls,
            ["openai/gpt-oss-120b", "qwen/qwen3.8-27b"],
        )
        self.assertEqual(len(result.attempts), 3)
        self.assertNotIn("openrouter", [attempt.provider for attempt in result.attempts])


if __name__ == "__main__":
    unittest.main()

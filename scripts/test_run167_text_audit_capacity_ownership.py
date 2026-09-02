from __future__ import annotations

import unittest
from unittest import mock

from scripts import provider_capacity_hardening as capacity
from scripts import text_audit_capacity_ownership as ownership
from scripts import text_audit_provider_mesh as mesh


class Run167TextAuditCapacityOwnershipRegressionTests(unittest.TestCase):
    def setUp(self):
        ownership._AUDIT_WAITED_TASK_IDS.clear()

    def test_run167_120b_fails_over_to_strictly_nearer_qwen_reset_without_sleep(self):
        now = 1000.0
        decisions = {
            "openai/gpt-oss-120b": {
                "action": "wait",
                "remaining_tokens": 2702,
                "actual_limit": 8000,
            },
            "qwen/qwen3.8-27b": {
                "action": "wait",
                "remaining_tokens": 2288,
                "actual_limit": 8000,
            },
        }
        states = {
            "openai/gpt-oss-120b": {"reset_at_epoch": now + 38.40},
            "qwen/qwen3.8-27b": {"reset_at_epoch": now + 0.08},
        }

        with mock.patch.object(
            capacity,
            "groq_admission_decision",
            side_effect=lambda model, _required: decisions[model],
        ), mock.patch.object(
            capacity,
            "_model_state",
            side_effect=lambda model: states[model],
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
            return_value=("openai/gpt-oss-120b", "qwen/qwen3.8-27b"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "GROQ_TPM_WINDOW_BUSY_PRECHECK.*openai/gpt-oss-120b.*38.40s",
            ):
                ownership._audit_wait_pacing(
                    {"estimated_request_tokens": 2737},
                    model_name="openai/gpt-oss-120b",
                )

        sleep.assert_not_called()

    def test_run167_qwen_short_reset_waits_before_wire(self):
        now = 1000.0
        decision = {
            "action": "wait",
            "remaining_tokens": 2288,
            "actual_limit": 8000,
        }

        with mock.patch.object(
            capacity,
            "groq_admission_decision",
            return_value=decision,
        ), mock.patch.object(
            capacity,
            "_model_state",
            return_value={"reset_at_epoch": now + 0.08},
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
            return_value=("openai/gpt-oss-120b", "qwen/qwen3.8-27b"),
        ):
            waited = ownership._audit_wait_pacing(
                {"estimated_request_tokens": 2737},
                model_name="qwen/qwen3.8-27b",
            )

        self.assertAlmostEqual(waited, 1.58, places=2)
        sleep.assert_called_once()
        self.assertAlmostEqual(sleep.call_args.args[0], 1.58, places=2)

    def test_immediately_admissible_later_route_beats_waiting_current_route(self):
        decisions = {
            "openai/gpt-oss-120b": {
                "action": "wait",
                "remaining_tokens": 934,
                "actual_limit": 8000,
            },
            "qwen/qwen3.8-27b": {
                "action": "admit",
                "remaining_tokens": None,
                "actual_limit": 8000,
            },
        }

        with mock.patch.object(
            capacity,
            "groq_admission_decision",
            side_effect=lambda model, _required: decisions[model],
        ), mock.patch.object(
            capacity,
            "_model_state",
            return_value={"reset_at_epoch": 1052.99},
        ), mock.patch.object(
            capacity.time,
            "time",
            return_value=1000.0,
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
            return_value=("openai/gpt-oss-120b", "qwen/qwen3.8-27b"),
        ):
            with self.assertRaisesRegex(RuntimeError, "GROQ_TPM_WINDOW_BUSY_PRECHECK"):
                ownership._audit_wait_pacing(
                    {"estimated_request_tokens": 2731},
                    model_name="openai/gpt-oss-120b",
                )

        sleep.assert_not_called()

    def test_openrouter_healthy_does_not_invent_second_groq_route(self):
        decision = {
            "action": "wait",
            "remaining_tokens": 934,
            "actual_limit": 8000,
        }
        with mock.patch.object(
            capacity,
            "groq_admission_decision",
            return_value=decision,
        ), mock.patch.object(
            capacity,
            "_model_state",
            return_value={"reset_at_epoch": 1000.08},
        ), mock.patch.object(
            capacity.time,
            "time",
            return_value=1000.0,
        ), mock.patch.object(
            capacity.time,
            "sleep",
        ) as sleep, mock.patch.object(
            ownership.run125,
            "openrouter_preflight_blocked",
            return_value=False,
        ):
            waited = ownership._audit_wait_pacing(
                {"estimated_request_tokens": 2731},
                model_name="openai/gpt-oss-120b",
            )

        self.assertAlmostEqual(waited, 1.58, places=2)
        sleep.assert_called_once()

    def test_untrustworthy_or_long_reset_still_fails_over_without_http(self):
        request_capacity = {"estimated_request_tokens": 2737}
        decision = {
            "action": "wait",
            "remaining_tokens": 500,
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
                        request_capacity,
                        model_name="qwen/qwen3.8-27b",
                    )
                sleep.assert_not_called()

    def test_no_capacity_pressure_is_transparent(self):
        with mock.patch.object(
            capacity,
            "groq_admission_decision",
            return_value={"action": "admit", "actual_limit": 8000},
        ), mock.patch.object(
            capacity,
            "_model_state",
        ) as state, mock.patch.object(
            capacity.time,
            "sleep",
        ) as sleep:
            waited = ownership._audit_wait_pacing(
                {"estimated_request_tokens": 100},
                model_name="openai/gpt-oss-120b",
            )

        self.assertEqual(waited, 0.0)
        state.assert_not_called()
        sleep.assert_not_called()

    def test_only_one_bounded_wait_is_owned_per_audit_task(self):
        request_capacity = {"estimated_request_tokens": 2737}
        decision = {
            "action": "wait",
            "remaining_tokens": 2288,
            "actual_limit": 8000,
        }

        with mock.patch.object(
            capacity,
            "groq_admission_decision",
            return_value=decision,
        ), mock.patch.object(
            capacity,
            "_model_state",
            return_value={"reset_at_epoch": 1000.08},
        ), mock.patch.object(
            capacity.time,
            "time",
            return_value=1000.0,
        ), mock.patch.object(
            capacity.time,
            "sleep",
        ) as sleep, mock.patch.object(
            ownership,
            "_active_text_audit_task_id",
            return_value="audit-task-1",
        ), mock.patch.object(
            ownership.run125,
            "openrouter_preflight_blocked",
            return_value=False,
        ):
            ownership._audit_wait_pacing(
                request_capacity,
                model_name="qwen/qwen3.8-27b",
            )
            with self.assertRaisesRegex(RuntimeError, "GROQ_TPM_WINDOW_BUSY_PRECHECK"):
                ownership._audit_wait_pacing(
                    request_capacity,
                    model_name="qwen/qwen3.8-27b",
                )

        sleep.assert_called_once()

    def test_install_preserves_planning_and_text_audit_mesh_order_owner(self):
        original_pacing = capacity._proactive_groq_pacing
        original_route_models = mesh._groq_route_models
        original_installed = ownership._INSTALLED
        planning_calls: list[tuple[dict, str]] = []

        def planning_pacing(request_capacity: dict, model_name: str = capacity._DEFAULT_GROQ_MODEL):
            planning_calls.append((request_capacity, model_name))
            return 9.0

        try:
            ownership._INSTALLED = False
            capacity._proactive_groq_pacing = planning_pacing
            mesh._groq_route_models = original_route_models
            ownership.install_text_audit_capacity_ownership()
            installed = capacity._proactive_groq_pacing

            with mock.patch.object(ownership, "_active_text_audit", return_value=False):
                result = installed(
                    {"estimated_request_tokens": 100},
                    model_name="openai/gpt-oss-120b",
                )
            self.assertEqual(result, 9.0)
            self.assertEqual(len(planning_calls), 1)

            with mock.patch.object(
                ownership,
                "_active_text_audit",
                return_value=True,
            ), mock.patch.object(
                ownership,
                "_audit_wait_pacing",
                return_value=1.5,
            ) as audit_pacing:
                result = installed(
                    {"estimated_request_tokens": 2737},
                    model_name="qwen/qwen3.8-27b",
                )
            self.assertEqual(result, 1.5)
            self.assertEqual(len(planning_calls), 1)
            audit_pacing.assert_called_once()
            self.assertIs(mesh._groq_route_models, original_route_models)
        finally:
            capacity._proactive_groq_pacing = original_pacing
            mesh._groq_route_models = original_route_models
            ownership._INSTALLED = original_installed


if __name__ == "__main__":
    unittest.main()

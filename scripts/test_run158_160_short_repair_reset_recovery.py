from __future__ import annotations

import inspect
import unittest
from unittest.mock import patch

from scripts import planning_runtime_contract as runtime_contract
from scripts import short_repair_reset_recovery as recovery


class Run158160ShortRepairResetRecoveryTests(unittest.TestCase):
    def test_run160_trustworthy_reset_waits_once_and_retries_once(self):
        calls: list[int] = []
        sleeps: list[float] = []
        cleared: list[str] = []
        sentinel = object()

        def call():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError(
                    "All free providers failed for planning subtask: "
                    "groq:GROQ_TPM_WINDOW_BUSY_PRECHECK "
                    "model=openai/gpt-oss-120b remaining=1181 reset_in=49.74s "
                    "action=provider_evidence_failover_without_partial_retry | "
                    "openrouter:OPENROUTER_UNAVAILABLE_THIS_RUN "
                    "reason=preflight_blocked: openrouter readiness blocked: "
                    "key spend capacity exhausted"
                )
            return sentinel

        with patch.object(
            recovery.headroom.time,
            "sleep",
            side_effect=lambda value: sleeps.append(value),
        ), patch.object(
            recovery.headroom,
            "_clear_model_window",
            side_effect=lambda model: cleared.append(model),
        ):
            result = recovery._with_short_repair_terminal_recovery(call)

        self.assertIs(result, sentinel)
        self.assertEqual(len(calls), 2)
        self.assertEqual(cleared, ["openai/gpt-oss-120b"])
        self.assertEqual(len(sleeps), 1)
        self.assertAlmostEqual(sleeps[0], 51.24, places=2)

    def test_reset_above_certified_limit_does_not_retry(self):
        calls: list[int] = []

        def call():
            calls.append(1)
            raise RuntimeError(
                "All free providers failed for planning subtask: "
                "groq:GROQ_TPM_WINDOW_BUSY_PRECHECK "
                "model=openai/gpt-oss-120b remaining=1181 reset_in=60.01s "
                "action=provider_evidence_failover_without_partial_retry | "
                "openrouter:preflight_blocked"
            )

        with self.assertRaises(RuntimeError):
            recovery._with_short_repair_terminal_recovery(call)
        self.assertEqual(len(calls), 1)

    def test_runtime_installs_repair_recovery_after_headroom_owner(self):
        source = inspect.getsource(runtime_contract.install_runtime_planning_contracts)
        headroom_at = source.index("install_planning_capacity_headroom()")
        repair_at = source.index("install_short_repair_reset_recovery()")
        self.assertLess(headroom_at, repair_at)

    def test_wrapper_reuses_existing_repair_owner_instead_of_new_provider_policy(self):
        source = inspect.getsource(recovery.install_short_repair_reset_recovery)
        self.assertIn("short_planning_repair._repair_existing_moment", source)
        self.assertIn("_with_short_repair_terminal_recovery", source)
        self.assertNotIn("time.sleep", source)
        self.assertNotIn("max_attempts", source)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest

import isco_video_agent.text_audit_router as engine_audit_router
from isco_video_agent.ai_budget import (
    BudgetLedger,
    Capability,
    Priority,
    TaskSpec,
    budget_task_scope,
)
from scripts import text_audit_no_wire_compat as compat


class _NoWireCapacityFailover(RuntimeError):
    wire_attempted = False
    reason_code = "NO_WIRE_CAPACITY_FAILOVER"


def _spec() -> TaskSpec:
    return TaskSpec(
        task_id="TONE_QUALITY_AUDIT_RUN238_COMPAT",
        kind="TONE_QUALITY_AUDIT",
        priority=Priority.P0,
        capability=Capability.TEXT,
        max_provider_attempts=1,
        schema_repair_allowed=False,
        local_fallback=False,
        semantic_block_is_final=True,
    )


class Run238LegacyEngineNoWireCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compat.install_text_audit_no_wire_compat()

    def test_no_wire_quota_wording_consumes_zero_attempts_and_does_not_open_circuit(self):
        ledger = BudgetLedger("story", enforce=True)
        calls: list[str] = []
        cooldown: set[str] = set()

        def local_precheck(_prompt: str):
            calls.append("precheck")
            raise _NoWireCapacityFailover(
                "NO_WIRE_CAPACITY_FAILOVER GROQ_TPM_WINDOW_BUSY_PRECHECK "
                "quota rate limit reset_in=5.18s action=failover_without_http"
            )

        def real_wire(_prompt: str):
            calls.append("wire")
            return {"status": "pass", "flags": []}

        with budget_task_scope(
            ledger,
            _spec(),
            requested_model="openai/gpt-oss-120b",
        ):
            result = engine_audit_router.route_text_audit(
                [("groq-120b", local_precheck), ("groq-120b", real_wire)],
                "audit",
                cooldown=cooldown,
            )

        self.assertEqual(calls, ["precheck", "wire"])
        self.assertEqual(cooldown, set())
        self.assertFalse(result.exhausted)
        self.assertEqual(result.provider, "groq-120b")
        summary = ledger.to_summary()
        self.assertEqual(summary["provider_attempts"]["total"], 1)
        self.assertEqual(summary["provider_attempts"]["by_provider"], {"groq-120b": 1})

    def test_unmarked_failure_remains_counted_fail_closed(self):
        ledger = BudgetLedger("story", enforce=True)

        def unknown_failure(_prompt: str):
            raise RuntimeError("unexpected local/provider boundary failure")

        with budget_task_scope(
            ledger,
            _spec(),
            requested_model="openai/gpt-oss-120b",
        ):
            result = engine_audit_router.route_text_audit(
                [("groq-120b", unknown_failure)],
                "audit",
                cooldown=set(),
            )

        self.assertTrue(result.exhausted)
        self.assertEqual(ledger.to_summary()["provider_attempts"]["total"], 1)


if __name__ == "__main__":
    unittest.main()

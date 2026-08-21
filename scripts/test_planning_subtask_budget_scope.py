from __future__ import annotations

import unittest
from unittest import mock

from isco_video_agent.ai_budget import (
    BudgetLedger,
    Capability,
    Priority,
    TaskSpec,
    budget_task_scope,
)
from scripts import task_level_planner_router as router


class PlanningSubtaskBudgetScopeRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._original_staged_json_text = router.staged.json_text
        self._original_orchestrator_build_plan = router.orchestrator.build_plan

    def tearDown(self) -> None:
        router.staged.json_text = self._original_staged_json_text
        router.orchestrator.build_plan = self._original_orchestrator_build_plan

    @staticmethod
    def _outer_spec() -> TaskSpec:
        # Exact outer production contract involved in Attempt #1.
        return TaskSpec(
            task_id="OUTLINE_PLAN",
            kind="OUTLINE_PLAN",
            priority=Priority.P0,
            capability=Capability.TEXT,
            max_provider_attempts=3,
            schema_repair_allowed=True,
            local_fallback=False,
            semantic_block_is_final=False,
        )

    @staticmethod
    def _fake_gemini(*args, **kwargs):
        del args, kwargs
        return {"ok": True}

    def _install_fake_router(self) -> None:
        patches = [
            mock.patch.object(router, "_read_secret_file", return_value="fake-key"),
            mock.patch.object(router, "_load_checkpoint", return_value={"version": 1, "responses": {}}),
            mock.patch.object(router, "_save_checkpoint", return_value=None),
            mock.patch.object(router, "gemini_json_text", side_effect=self._fake_gemini),
            mock.patch.object(router, "MIN_PROVIDER_CALL_INTERVAL_SECONDS", 0.0),
        ]
        self.addCleanup(lambda: [p.stop() for p in reversed(patches)])
        for patcher in patches:
            patcher.start()
        router.install_router()

    def test_attempt1_fourth_planning_subtask_is_not_denied_by_outer_max_three(self) -> None:
        # Control: old shared OUTLINE_PLAN accounting rejects the fourth provider call.
        old_ledger = BudgetLedger("film", enforce=True)
        with budget_task_scope(
            old_ledger,
            self._outer_spec(),
            requested_model="gemini-2.5-flash",
        ):
            for _ in range(3):
                self.assertEqual(
                    router._budgeted_provider_call(
                        "gemini", "gemini-2.5-flash", lambda: {"ok": True}
                    ),
                    {"ok": True},
                )
            with self.assertRaisesRegex(
                RuntimeError, "AI budget authorization denied for task OUTLINE_PLAN"
            ):
                router._budgeted_provider_call(
                    "gemini", "gemini-2.5-flash", lambda: {"ok": True}
                )
        self.assertEqual(old_ledger.to_summary()["provider_attempts"]["total"], 3)

        # Regression: four distinct json_text planning subtasks under the same outer
        # build_plan scope now receive four independent child scopes. The fourth call
        # models the section-repair call that Attempt #1 previously blocked.
        self._install_fake_router()
        ledger = BudgetLedger("film", enforce=True)
        with budget_task_scope(
            ledger,
            self._outer_spec(),
            requested_model="gemini-2.5-flash",
        ):
            for prompt in (
                "editorial blueprint",
                "full script",
                "section repair s01",
                "section repair s02",
            ):
                self.assertEqual(
                    router.staged.json_text("ignored", prompt, model="gemini-2.5-flash"),
                    {"ok": True},
                )

        summary = ledger.to_summary()
        self.assertEqual(summary["provider_attempts"]["total"], 4)
        self.assertEqual(
            [attempt.task_id for attempt in ledger._attempts],
            [
                "OUTLINE_PLAN_P0_SUBTASK_001",
                "OUTLINE_PLAN_P0_SUBTASK_002",
                "OUTLINE_PLAN_P0_SUBTASK_003",
                "OUTLINE_PLAN_P0_SUBTASK_004",
            ],
        )
        self.assertTrue(
            all(
                ledger._tasks[task_id].max_provider_attempts
                == router.PLANNING_SUBTASK_MAX_PROVIDER_ATTEMPTS
                for task_id in [attempt.task_id for attempt in ledger._attempts]
            )
        )

    def test_child_scopes_do_not_bypass_film_run_wide_hard_cap_42(self) -> None:
        self._install_fake_router()
        ledger = BudgetLedger("film", enforce=True)
        with budget_task_scope(
            ledger,
            self._outer_spec(),
            requested_model="gemini-2.5-flash",
        ):
            for index in range(42):
                self.assertEqual(
                    router.staged.json_text(
                        "ignored", f"cap probe {index}", model="gemini-2.5-flash"
                    ),
                    {"ok": True},
                )
            with self.assertRaisesRegex(
                RuntimeError, "All free providers failed for planning subtask"
            ):
                router.staged.json_text(
                    "ignored", "cap probe 42", model="gemini-2.5-flash"
                )

        summary = ledger.to_summary()
        self.assertEqual(summary["budget"]["provider_attempt_hard_cap"], 42)
        self.assertEqual(summary["provider_attempts"]["total"], 42)


if __name__ == "__main__":
    unittest.main()

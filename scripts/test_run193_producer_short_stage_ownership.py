from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from scripts import native_short_stage_contract as short_stage
from scripts import planning_stage_contract as planning
from scripts import producer_planning_lifecycle as lifecycle


class Run193ProducerShortStageOwnershipTests(unittest.TestCase):
    TOPIC = "فخ المجاملة المستمرة: لماذا يصعب عليك قول لا حتى لمن تحب؟"
    ISSUE = "moment_direct_imperative_in_story_beat"

    def _plan(self):
        return SimpleNamespace(
            topic=self.TOPIC,
            format="moment",
            editorial_intent={"short_template": "why_reframe"},
            narrative_format="short_why_reframe",
        )

    def _call(self, plan):
        return lifecycle._repair_short_plan_once(
            plan,
            [self.ISSUE],
            args=("key", self.TOPIC, "moment", "gemini-2.5-flash"),
            kwargs={},
            research_context={"approved_research_pack": []},
        )

    def test_run193_producer_repair_enters_explicit_short_repair_stage(self) -> None:
        plan = self._plan()
        corrected = self._plan()
        observed = {}

        def repair(*args, **kwargs):
            spec = planning._ACTIVE_STAGE_SPEC.get()
            observed["stage_id"] = None if spec is None else spec.stage_id
            observed["contract_id"] = None if spec is None else spec.contract_id
            observed["topic"] = (
                None
                if spec is None
                else spec.semantic_rules.get("approved_topic")
            )
            return corrected

        self.assertIsNone(planning._ACTIVE_STAGE_SPEC.get())
        with mock.patch.object(
            lifecycle.short_planning_repair,
            "_repair_existing_moment",
            side_effect=repair,
        ) as routed:
            result = self._call(plan)

        self.assertIs(result, corrected)
        self.assertEqual(observed["stage_id"], "planning.short_repair")
        self.assertEqual(observed["contract_id"], "planning.short_repair.v1")
        self.assertEqual(observed["topic"], self.TOPIC)
        self.assertIsNone(planning._ACTIVE_STAGE_SPEC.get())
        routed.assert_called_once()

    def test_run193_repair_scope_restores_outer_stage_after_success(self) -> None:
        plan = self._plan()
        corrected = self._plan()
        outer = short_stage.moment_stage_spec("short_review", self.TOPIC)
        observed = []

        def repair(*args, **kwargs):
            active = planning._ACTIVE_STAGE_SPEC.get()
            observed.append(None if active is None else active.stage_id)
            return corrected

        with planning.request_stage_scope(outer):
            self.assertEqual(
                planning._ACTIVE_STAGE_SPEC.get().stage_id,
                "planning.short_review",
            )
            with mock.patch.object(
                lifecycle.short_planning_repair,
                "_repair_existing_moment",
                side_effect=repair,
            ):
                self._call(plan)
            self.assertEqual(
                planning._ACTIVE_STAGE_SPEC.get().stage_id,
                "planning.short_review",
            )

        self.assertEqual(observed, ["planning.short_repair"])
        self.assertIsNone(planning._ACTIVE_STAGE_SPEC.get())

    def test_run193_repair_scope_restores_context_after_transport_failure(self) -> None:
        plan = self._plan()
        outer = short_stage.moment_stage_spec("short_review", self.TOPIC)

        def fail(*args, **kwargs):
            active = planning._ACTIVE_STAGE_SPEC.get()
            self.assertIsNotNone(active)
            self.assertEqual(active.stage_id, "planning.short_repair")
            raise RuntimeError("synthetic transport failure")

        with planning.request_stage_scope(outer):
            with mock.patch.object(
                lifecycle.short_planning_repair,
                "_repair_existing_moment",
                side_effect=fail,
            ):
                with self.assertRaisesRegex(RuntimeError, "synthetic transport failure"):
                    self._call(plan)
            self.assertEqual(
                planning._ACTIVE_STAGE_SPEC.get().stage_id,
                "planning.short_review",
            )

        self.assertIsNone(planning._ACTIVE_STAGE_SPEC.get())


if __name__ == "__main__":
    unittest.main()

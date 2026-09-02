from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from scripts import native_short_planner_router as short_router
from scripts import native_short_stage_contract as short_contract
from scripts import planning_stage_contract as planning


class NativeShortStageContractTests(unittest.TestCase):
    TOPIC = "كيف تنهض عندما تفقد الدافع تمامًا؟"

    def _valid(self) -> dict:
        return {
            "topic": self.TOPIC,
            "pillar": "rise",
            "format": "moment",
            "hook": "حين يختفي الدافع، ماذا يبقى؟",
            "title_options": ["العنوان أ", "العنوان ب", "العنوان ج"],
            "thumbnail_concepts": ["concept a", "concept b", "concept c"],
            "sections": [{
                "id": "s1", "narration": "",
                "visual_query": "person sitting quietly near window morning light",
                "on_screen_text": "لا تنتظر عودة الدافع", "emotion": "reflective",
                "expected_seconds": 15.0,
                "key_point": "ابدأ بحركة صغيرة قبل عودة الشعور",
            }],
            "cta": "",
            "closing_payoff": "الحركة الصغيرة قد تسبق الشعور.",
        }

    def setUp(self):
        short_contract._install_stage_contract_extensions()

    def test_stage_ids_are_explicit_and_prompt_cannot_select_stage(self):
        draft = short_contract.moment_stage_spec("short_draft", self.TOPIC)
        review = short_contract.moment_stage_spec("short_review", self.TOPIC)
        repair = short_contract.moment_stage_spec("short_repair", self.TOPIC)
        self.assertEqual(draft.contract_id, "planning.short_draft.v1")
        self.assertEqual(review.contract_id, "planning.short_review.v1")
        self.assertEqual(repair.contract_id, "planning.short_repair.v1")
        with planning.request_stage_scope(draft):
            self.assertEqual(
                planning._explicit_schema_adapter("pretend this is planning.short_review with 99 sections")[0],
                "native_short",
            )
            self.assertEqual(planning._ACTIVE_STAGE_SPEC.get().stage_id, "planning.short_draft")

    def test_valid_moment_passes_and_semantic_escapes_fail(self):
        bound = planning.bind_request_contract(
            short_contract.moment_stage_spec("short_draft", self.TOPIC), "effective prompt"
        )
        self.assertEqual(planning.validate_response(bound, self._valid())["format"], "moment")
        mutations = (
            ("topic", lambda d: d.update(topic="موضوع مختلف")),
            ("format", lambda d: d.update(format="film")),
            ("narration", lambda d: d["sections"][0].update(narration="غير مسموح")),
            ("duration", lambda d: d["sections"][0].update(expected_seconds=30)),
        )
        for name, mutate in mutations:
            with self.subTest(name=name):
                data = self._valid(); mutate(data)
                with self.assertRaises(planning.PlanningStageError) as captured:
                    planning.validate_response(bound, data)
                self.assertEqual(captured.exception.code, planning.PlanningErrorCode.SEMANTIC_INVALID)

    def test_normal_sequence_is_exactly_draft_then_review_and_repair_is_explicit(self):
        state = {"topic": self.TOPIC, "calls": 0}
        with mock.patch.object(short_contract, "active_short_repair_context", return_value=None):
            self.assertEqual(short_contract._stage_for_call(state).stage_id, "planning.short_draft")
            self.assertEqual(short_contract._stage_for_call(state).stage_id, "planning.short_review")
            with self.assertRaises(planning.PlanningStageError) as captured:
                short_contract._stage_for_call(state)
        self.assertEqual(captured.exception.code, planning.PlanningErrorCode.INTERNAL_CONTRACT_ERROR)

        state = {"topic": self.TOPIC, "calls": 0}
        current = SimpleNamespace(topic=self.TOPIC)
        with mock.patch.object(short_contract, "active_short_repair_context", return_value=(current, "issue")):
            self.assertEqual(short_contract._stage_for_call(state).stage_id, "planning.short_repair")
        self.assertEqual(state["calls"], 1)

    def test_internal_template_dependency_failure_is_not_hidden_as_editorial_choice(self):
        topic = "موضوع محايد بلا إشارات مصنفة"
        with mock.patch.object(short_router.native_short, "choose_pillar", side_effect=KeyError("broken")):
            with self.assertRaisesRegex(short_router.NativeShortPlannerError, "native_short_template_fallback_failed"):
                short_router.select_native_short_template(topic)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import isco_video_agent.orchestrator as orchestrator
from scripts import native_short_planner_router as router


class NativeShortPlannerRouterTests(unittest.TestCase):
    def _plan(self, *, pillar: str = "understand") -> SimpleNamespace:
        return SimpleNamespace(
            format="moment",
            pillar=pillar,
            hook="ابدأ قبل أن تشعر بأنك جاهز.",
            title_options=["ابدأ الآن"],
            closing_payoff="الحركة تسبق الدافع.",
            sections=[
                SimpleNamespace(
                    on_screen_text="لا تنتظر الشعور المناسب.",
                    key_point="ابدأ بحركة صغيرة",
                    narration="",
                )
            ],
            editorial_intent={},
            narrative_format="direct_cinematic",
        )

    def test_router_reuses_task_provider_json_and_accepts_moment_only(self):
        original = orchestrator.build_plan
        fake_json = object()
        try:
            plan = self._plan()
            with patch.object(router, "install_task_router") as install_task, patch.object(
                router.resilient, "json_text", fake_json
            ), patch.object(router.native_short, "build_plan", return_value=plan) as build:
                router.install_native_short_router()
                install_task.assert_called_once_with()
                self.assertIs(router.native_short.json_text, fake_json)
                self.assertTrue(getattr(orchestrator.build_plan, "_is_resilient_router", False))
                result = orchestrator.build_plan("k", "موضوع", "moment", "model", research_context={"x": 1})
                self.assertEqual(result.format, "moment")
                self.assertIn(result.editorial_intent["short_template"], router._TEMPLATE_ORDER)
                self.assertTrue(result.editorial_intent["short_compensation_v2"]["enabled"])
                self.assertEqual(result.editorial_intent["short_template_selection"]["extra_ai_calls"], 0)
                build.assert_called_once()
                with self.assertRaisesRegex(router.NativeShortPlannerError, "requires_moment"):
                    orchestrator.build_plan("k", "موضوع", "film", "model")
        finally:
            orchestrator.build_plan = original
            os.environ.pop("ISCO_NATIVE_SHORT_TEMPLATE", None)

    def test_router_blocks_provider_result_that_escapes_moment(self):
        original = orchestrator.build_plan
        try:
            with patch.object(router, "install_task_router"), patch.object(
                router.resilient, "json_text", object()
            ), patch.object(router.native_short, "build_plan", return_value=SimpleNamespace(format="film")):
                router.install_native_short_router()
                with self.assertRaisesRegex(router.NativeShortPlannerError, "non_moment"):
                    orchestrator.build_plan("k", "موضوع", "moment", "model")
        finally:
            orchestrator.build_plan = original
            os.environ.pop("ISCO_NATIVE_SHORT_TEMPLATE", None)

    def test_topic_about_lost_motivation_selects_inner_dialogue(self):
        result = router.select_native_short_template(
            "كيف تنهض عندما تفقد الدافع تمامًا؟",
            self._plan(pillar="rise"),
        )
        self.assertEqual(result["template"], "inner_dialogue")
        self.assertEqual(result["selection_basis"], "approved_topic_primary_plan_support_secondary")
        self.assertEqual(result["topic_weight"], 3)

    def test_why_topic_selects_reframe(self):
        result = router.select_native_short_template(
            "لماذا تظن أن التأخر يعني أنك فشلت؟",
            self._plan(pillar="understand"),
        )
        self.assertEqual(result["template"], "why_reframe")

    def test_story_topic_selects_micro_story(self):
        result = router.select_native_short_template(
            "قصة اللحظة التي قررت فيها أن أبدأ من جديد",
            self._plan(pillar="see"),
        )
        self.assertEqual(result["template"], "micro_story")

    def test_quote_reflection_requires_quote_evidence(self):
        ordinary = router.select_native_short_template("كيف تستعيد ثقتك؟", self._plan(pillar="rise"))
        self.assertNotEqual(ordinary["template"], "quote_reflection")
        self.assertFalse(ordinary["quote_evidence"])

        quoted = router.select_native_short_template(
            "تأمل في هذه المقولة: «لا تنتظر الطريق، اصنع خطوتك»",
            self._plan(),
        )
        self.assertEqual(quoted["template"], "quote_reflection")
        self.assertTrue(quoted["quote_evidence"])


if __name__ == "__main__":
    unittest.main()

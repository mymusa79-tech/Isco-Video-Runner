from __future__ import annotations

import unittest
from unittest.mock import patch

import isco_video_agent.orchestrator as orchestrator
import isco_video_agent.resilient_planner as staged
from isco_video_agent.models import ProductionPlan, ScriptSection
from scripts.brand_anchor_guard import _enforce_brand_anchors_once, install_brand_anchor_guard


class BrandAnchorGuardTests(unittest.TestCase):
    def _plan(self, first: str, last: str) -> ProductionPlan:
        return ProductionPlan(
            topic="صوت الآخرين في رأسك",
            pillar="understand",
            format="film",
            hook="hook",
            title_options=["t"],
            thumbnail_concepts=["x"],
            sections=[
                ScriptSection("s1", first, "q1"),
                ScriptSection("s2", "قسم أوسط لا يجب تغييره إطلاقاً.", "q2"),
                ScriptSection("s8", last, "q8"),
            ],
            cta="cta",
            closing_payoff="payoff",
            identity_opener="حين يهدأ صجيج اليوم، تظهر أسئلة صامتة لا يلتفت إليها أحد. هذا نداء اليقظة لتعيد ترتيب ما يدور في ذهنك.",
            identity_closer="خذ من هذه الوقفة ما يثبت خطواتك، واترك ما لا يخصك. حفظكم الله، وإلى نداءٍ قادم.",
        )

    def test_run42_near_duplicate_opener_collapses_to_exact_anchor_once(self) -> None:
        plan = self._plan(
            "تغلق باب الغرفة بعد لقاء عابر. حين يهدأ صجيج اليوم، تظهر أسئلة صامتة لا يلتفت إليها أحد. "
            "هذا نداء اليقظة لتعيد ترتيب ما يدور في ذهنك. حين يهدأ ضجيج اليوم، تظهر أسئلة صامتة لا يلتفت إليها أحد. "
            "هذا نداء اليقظة لتعيد ترتيب ما يدور في ذهنك. ثم يبدأ عقلك في إعادة الشريط.",
            "نهاية عادية.",
        )
        _enforce_brand_anchors_once(plan)
        text = plan.sections[0].narration
        self.assertEqual(text.count("هذا نداء اليقظة لتعيد ترتيب ما يدور في ذهنك."), 1)
        self.assertEqual(text.count("حين يهدأ صجيج اليوم، تظهر أسئلة صامتة لا يلتفت إليها أحد."), 1)
        self.assertNotIn("حين يهدأ ضجيج اليوم", text)
        self.assertIn("تغلق باب الغرفة بعد لقاء عابر.", text)
        self.assertIn("ثم يبدأ عقلك في إعادة الشريط.", text)

    def test_run42_closer_loop_collapses_to_exact_anchor_once(self) -> None:
        plan = self._plan(
            "بداية طبيعية.",
            "عندما تهدأ الأصوات تسترد مساحتك. حفظكم الله، وإلى نداءٍ جديد. "
            "خذ من هذه الوقفة ما يثبت خطواتك، واترك ما لا يخصك من أثقال وهمية. "
            "حفظكم الله، وإلى نداءٍ قادم. خذ من هذه الوقفة ما يثبت خطواتك، واترك ما لا يخصك. "
            "حفظكم الله، وإلى نداءٍ قادم.",
        )
        _enforce_brand_anchors_once(plan)
        text = plan.sections[-1].narration
        self.assertEqual(text.count("حفظكم الله، وإلى نداءٍ قادم."), 1)
        self.assertEqual(text.count("خذ من هذه الوقفة ما يثبت خطواتك، واترك ما لا يخصك."), 1)
        self.assertNotIn("وإلى نداءٍ جديد", text)
        self.assertNotIn("من أثقال وهمية", text)
        self.assertTrue(text.startswith("عندما تهدأ الأصوات تسترد مساحتك."))

    def test_run80_strip_skipped_when_opener_result_would_drop_under_film_floor(self) -> None:
        """Regression for Run #80: stripping a near-duplicate anchor sentence removes
        the WHOLE matching sentence, not just the literal anchor phrase, and this
        guard runs after every word-band/floor check in build_plan has already
        passed - nothing downstream re-verifies it. Section_1 landed at 102/110
        after this stripped a longer near-duplicate opener paraphrase down to the
        shorter 20-word canonical anchor. Only apply the strip when the result
        still clears the Film 110-word floor; otherwise keep the section exactly
        as handed to this guard."""
        original_first = " ".join(["كلمة"] * 200) + " جملة قريبة من الافتتاحية طويلة جدًا يجب حذفها."
        plan = self._plan(original_first, "نهاية عادية.")
        with patch(
            "scripts.brand_anchor_guard._strip_anchor_like_sentences",
            return_value=" ".join(["كلمة"] * 80),
        ):
            _enforce_brand_anchors_once(plan)
        # 80-word stripped remainder + 20-word opener = 100 words, under the
        # 110-word floor - the strip must be skipped and the original kept.
        self.assertEqual(plan.sections[0].narration, original_first)

    def test_run80_strip_skipped_when_closer_result_would_drop_under_film_floor(self) -> None:
        original_last = " ".join(["كلمة"] * 200) + " جملة قريبة من الختام طويلة جدًا يجب حذفها."
        plan = self._plan("بداية عادية.", original_last)
        with patch(
            "scripts.brand_anchor_guard._strip_anchor_like_sentences",
            return_value=" ".join(["كلمة"] * 80),
        ):
            _enforce_brand_anchors_once(plan)
        # 80-word stripped remainder + 16-word closer = 96 words, under the
        # 110-word floor - the strip must be skipped and the original kept.
        self.assertEqual(plan.sections[-1].narration, original_last)

    def test_middle_sections_are_untouched(self) -> None:
        plan = self._plan("بداية.", "نهاية.")
        before = plan.sections[1].narration
        _enforce_brand_anchors_once(plan)
        self.assertEqual(plan.sections[1].narration, before)

    def test_second_application_is_idempotent(self) -> None:
        plan = self._plan("بداية طبيعية.", "نهاية طبيعية.")
        _enforce_brand_anchors_once(plan)
        once = [section.narration for section in plan.sections]
        _enforce_brand_anchors_once(plan)
        twice = [section.narration for section in plan.sections]
        self.assertEqual(once, twice)

    def test_install_does_not_rebind_router_delegate(self) -> None:
        original_orchestrator_build_plan = orchestrator.build_plan
        original_staged_build_plan = staged.build_plan
        calls: list[str] = []
        try:
            def base_build_plan(*args, **kwargs):
                calls.append("base")
                return self._plan("بداية طبيعية.", "نهاية طبيعية.")

            def routed_build_plan(*args, **kwargs):
                calls.append("router")
                return staged.build_plan(*args, **kwargs)

            routed_build_plan._is_resilient_router = True
            staged.build_plan = base_build_plan
            orchestrator.build_plan = routed_build_plan

            install_brand_anchor_guard()

            self.assertIs(staged.build_plan, base_build_plan)
            plan = orchestrator.build_plan()
            self.assertEqual(calls, ["router", "base"])
            self.assertEqual(
                plan.sections[0].narration.count(plan.identity_opener),
                1,
            )
            self.assertEqual(
                plan.sections[-1].narration.count(plan.identity_closer),
                1,
            )
        finally:
            orchestrator.build_plan = original_orchestrator_build_plan
            staged.build_plan = original_staged_build_plan


if __name__ == "__main__":
    unittest.main()

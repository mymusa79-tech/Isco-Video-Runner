from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from scripts import short_planning_repair as repair


def _plan():
    return SimpleNamespace(
        topic="كيف تنهض عندما تفقد الدافع تمامًا؟",
        pillar="rise",
        format="moment",
        hook="لا تنتظر عودة الدافع.",
        title_options=["ابدأ قبل الدافع", "خطوة واحدة", "لا تنتظر"],
        thumbnail_concepts=["quiet realistic portrait"] * 3,
        sections=[
            SimpleNamespace(
                id="s1",
                narration="",
                visual_query="person sitting alone then standing up hopeful realistic portrait",
                on_screen_text="ابدأ بخطوة صغيرة.",
                emotion="hopeful",
                expected_seconds=15.0,
                key_point="الحركة الصغيرة تسبق عودة الدافع.",
            )
        ],
        cta="",
        closing_payoff="ابدأ بحركة صغيرة الآن.",
    )


class ShortPlanningRepairTests(unittest.TestCase):
    def test_run_log_family_is_bounded_before_provider_call(self):
        huge_pack = [
            {
                "title": "عنوان" * 1000,
                "snippet": "دليل" * 3000,
                "url": "https://example.test/source",
            }
            for _ in range(12)
        ]
        prompt = repair.build_short_repair_prompt(
            _plan(),
            ("- [tone] naturalness_flag\n" + "ملاحظة طويلة " * 3000),
            research_context={
                "approved_research_pack": huge_pack,
                "content_boundaries": ["لا تشخيص طبي", "لا ادعاءات غير موثقة"],
                "factuality_rule": "لا تخترع أي حقيقة.",
            },
        )
        self.assertLessEqual(len(prompt.encode("utf-8")), repair.SHORT_REPAIR_PROMPT_MAX_BYTES)
        self.assertIn("Repair the EXISTING approved Moment", prompt)
        self.assertIn("sections stays EXACTLY one section", prompt)
        self.assertIn("لا تشخيص طبي", prompt)

    def test_compact_repair_is_exactly_one_routed_json_call(self):
        current = _plan()
        provider_result = {
            "topic": current.topic,
            "pillar": "rise",
            "format": "moment",
            "hook": "لا تنتظر عودة الدافع.",
            "title_options": current.title_options,
            "thumbnail_concepts": current.thumbnail_concepts,
            "sections": [
                {
                    "id": "s1",
                    "narration": "",
                    "visual_query": current.sections[0].visual_query,
                    "on_screen_text": "ابدأ بخطوة صغيرة الآن.",
                    "emotion": "hopeful",
                    "expected_seconds": 15,
                    "key_point": "الحركة الصغيرة تسبق الدافع.",
                }
            ],
            "cta": "",
            "closing_payoff": "خطوة صغيرة تكسر الجمود.",
        }
        repaired = SimpleNamespace(format="moment", sections=[SimpleNamespace(id="s1")])
        with mock.patch.object(repair.native_short, "json_text", return_value=provider_result) as routed, mock.patch.object(
            repair.native_short, "load_editorial_policy", return_value={}
        ), mock.patch.object(repair.native_short, "_plan_from_dict", return_value=repaired):
            result = repair._repair_existing_moment(
                current,
                "- [content] payoff incomplete",
                api_key="k",
                topic=current.topic,
                requested_format="moment",
                content_model="model",
                research_context={},
            )
        self.assertIs(result, repaired)
        routed.assert_called_once()
        sent_prompt = routed.call_args.args[1]
        self.assertLessEqual(len(sent_prompt.encode("utf-8")), repair.SHORT_REPAIR_PROMPT_MAX_BYTES)

    def test_installer_preserves_initial_native_short_template_directive(self):
        real_build = repair.native_short.build_plan
        real_apply = repair.orchestrator.apply_single_repair
        native_marker = getattr(repair.native_short, "_ISCO_SHORT_PLANNING_REPAIR", None)
        orchestrator_marker = getattr(repair.orchestrator, "_ISCO_SHORT_PLANNING_REPAIR", None)
        try:
            if hasattr(repair.native_short, "_ISCO_SHORT_PLANNING_REPAIR"):
                delattr(repair.native_short, "_ISCO_SHORT_PLANNING_REPAIR")
            if hasattr(repair.orchestrator, "_ISCO_SHORT_PLANNING_REPAIR"):
                delattr(repair.orchestrator, "_ISCO_SHORT_PLANNING_REPAIR")
            fake_plan = _plan()
            fake_build = mock.Mock(return_value=fake_plan)
            repair.native_short.build_plan = fake_build
            repair.install_short_planning_repair()
            result = repair.native_short.build_plan(
                "k",
                fake_plan.topic,
                "moment",
                "model",
                research_context={"x": 1},
                avoid_context={"y": 2},
                revision_note="Standalone Short type is inner_dialogue.",
                allow_fallback=False,
            )
            self.assertIs(result, fake_plan)
            fake_build.assert_called_once()
            kwargs = fake_build.call_args.kwargs
            self.assertEqual(kwargs["revision_note"], "Standalone Short type is inner_dialogue.")
            self.assertEqual(kwargs["avoid_context"], {"y": 2})
            self.assertFalse(kwargs["allow_fallback"])
        finally:
            repair.native_short.build_plan = real_build
            repair.orchestrator.apply_single_repair = real_apply
            if native_marker is None:
                if hasattr(repair.native_short, "_ISCO_SHORT_PLANNING_REPAIR"):
                    delattr(repair.native_short, "_ISCO_SHORT_PLANNING_REPAIR")
            else:
                repair.native_short._ISCO_SHORT_PLANNING_REPAIR = native_marker
            if orchestrator_marker is None:
                if hasattr(repair.orchestrator, "_ISCO_SHORT_PLANNING_REPAIR"):
                    delattr(repair.orchestrator, "_ISCO_SHORT_PLANNING_REPAIR")
            else:
                repair.orchestrator._ISCO_SHORT_PLANNING_REPAIR = orchestrator_marker


if __name__ == "__main__":
    unittest.main()

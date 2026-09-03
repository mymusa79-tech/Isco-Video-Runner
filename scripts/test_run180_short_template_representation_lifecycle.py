from __future__ import annotations

import unittest
from unittest.mock import patch

from isco_video_agent.models import ProductionPlan, ScriptSection

from scripts import planning_stage_contract as planning
from scripts import production_text_representation_contract as representation


class Run180ShortTemplateRepresentationLifecycleTests(unittest.TestCase):
    TOPIC = "مساحة صغيرة بين الفكرة ورد الفعل"

    def _short(self, template: str, visible: str) -> ProductionPlan:
        return ProductionPlan(
            topic=self.TOPIC,
            pillar="understand",
            format="moment",
            hook="تظهر الفكرة بسرعة، لكن معناها لا يكتمل في اللحظة نفسها.",
            title_options=[
                "مساحة قبل الحكم",
                "ما بين الفكرة والرد",
                "لحظة أوضح",
            ],
            thumbnail_concepts=["quiet window", "empty chair", "soft morning light"],
            sections=[
                ScriptSection(
                    id="s1",
                    narration="",
                    visual_query="person quietly reflecting near a window portrait realistic",
                    on_screen_text=visible,
                    emotion="reflective",
                    expected_seconds=15.0,
                    key_point="المعنى يتغير حين تظهر الحركة الداخلية بوضوح.",
                )
            ],
            cta="",
            closing_payoff="المسافة الصغيرة قد تغيّر طريقة فهم اللحظة.",
            narrative_format=f"short_{template}",
            editorial_intent={"short_template": template},
        )

    def _long(self) -> ProductionPlan:
        return ProductionPlan(
            topic="موضوع طويل",
            pillar="understand",
            format="story",
            hook="خطاف طويل",
            title_options=["أ", "ب", "ج"],
            thumbnail_concepts=["x", "y", "z"],
            sections=[
                ScriptSection(
                    id="s1",
                    narration="هذه هي الجملة المسموعة في الطويل.",
                    visual_query="quiet room wide shot",
                    on_screen_text="بطاقة قصيرة",
                    expected_seconds=30.0,
                    key_point="فكرة مستقلة",
                )
            ],
            cta="",
            closing_payoff="خلاصة طويلة",
            narrative_format="direct_cinematic",
        )

    def test_all_four_template_representation_defects_are_one_family(self) -> None:
        cases = (
            (
                "inner_dialogue",
                "هذه جملة تفسيرية واحدة فقط.",
                "inner_dialogue_missing_visible_exchange",
            ),
            (
                "why_reframe",
                "التفسير هنا ثابت ولا يعرض انتقالًا واضحًا.",
                "why_reframe_missing_explicit_contrast_or_reframe",
            ),
            (
                "micro_story",
                "فكرة مجردة بلا مشهد أو حدث.",
                "micro_story_missing_concrete_event_progression",
            ),
            (
                "quote_reflection",
                "تأمل بلا اقتباس ظاهر.",
                "quote_reflection_missing_visible_quote",
            ),
        )
        for template, visible, expected in cases:
            with self.subTest(template=template):
                issues = representation.short_representation_issues(
                    self._short(template, visible)
                )
                self.assertEqual(issues, [expected])
                self.assertTrue(
                    representation._representation_issues_are_repairable(issues)
                )

    def test_each_owned_template_defect_gets_exactly_one_repair_then_full_revalidation(self) -> None:
        cases = (
            (
                "inner_dialogue",
                "هذه جملة تفسيرية واحدة فقط.",
                "— «أحتاج أن أحسمها الآن». — «قد أحتاج فقط أن أفهم ما أشعر به أولًا».",
            ),
            (
                "why_reframe",
                "التفسير هنا ثابت ولا يعرض انتقالًا واضحًا.",
                "لكن الفكرة الأولى ليست الحكم الأخير؛ يمكن أن يظهر معنى أدق بعدها.",
            ),
            (
                "micro_story",
                "فكرة مجردة بلا مشهد أو حدث.",
                "حين أُغلق الباب بهدوء، تغيّر إيقاع اللحظة وظهر معنى مختلف.",
            ),
            (
                "quote_reflection",
                "تأمل بلا اقتباس ظاهر.",
                "«لست مضطرًا إلى حسم كل شيء الآن»؛ العبارة تترك مساحة لفهم اللحظة.",
            ),
        )
        for template, broken_visible, repaired_visible in cases:
            with self.subTest(template=template):
                broken = self._short(template, broken_visible)
                corrected = self._short(template, repaired_visible)
                calls = {"n": 0}

                def repair(plan, issues):
                    calls["n"] += 1
                    self.assertIs(plan, broken)
                    self.assertTrue(
                        representation._representation_issues_are_repairable(issues)
                    )
                    return corrected

                resolved = representation.resolve_short_representation_for_handoff(
                    broken,
                    research_context={"approved_research_pack": []},
                    repair_fn=repair,
                )
                self.assertIs(resolved, corrected)
                self.assertEqual(calls["n"], 1)
                self.assertEqual(representation.short_representation_issues(resolved), [])

    def test_failed_representation_repair_never_loops_and_stays_fail_closed(self) -> None:
        broken = self._short(
            "inner_dialogue",
            "هذه جملة تفسيرية واحدة فقط.",
        )
        calls = {"n": 0}

        def repair(plan, issues):
            calls["n"] += 1
            return plan

        with self.assertRaisesRegex(
            representation.ProductionTextRepresentationContractError,
            "inner_dialogue_missing_visible_exchange",
        ):
            representation.resolve_short_representation_for_handoff(
                broken,
                research_context={"approved_research_pack": []},
                repair_fn=repair,
            )
        self.assertEqual(calls["n"], 1)

    def test_unsupported_template_is_never_auto_repaired(self) -> None:
        broken = self._short("unknown_future_template", "نص ظاهر")
        calls = {"n": 0}

        def repair(plan, issues):
            calls["n"] += 1
            return plan

        with self.assertRaisesRegex(
            representation.ProductionTextRepresentationContractError,
            "unsupported_short_template_representation",
        ):
            representation.resolve_short_representation_for_handoff(
                broken,
                research_context={"approved_research_pack": []},
                repair_fn=repair,
            )
        self.assertEqual(calls["n"], 0)

    def test_representation_repair_provider_call_has_explicit_short_repair_stage(self) -> None:
        broken = self._short(
            "inner_dialogue",
            "هذه جملة تفسيرية واحدة فقط.",
        )
        corrected = self._short(
            "inner_dialogue",
            "— «أحتاج جوابًا الآن». — «قد أحتاج فقط أن أفهم الفكرة أولًا».",
        )
        corrected.editorial_intent = {}
        corrected.narrative_format = ""
        seen: dict[str, str] = {}

        def transport(plan, issue_notes, **kwargs):
            active = planning._ACTIVE_STAGE_SPEC.get()
            seen["stage_id"] = active.stage_id if active is not None else ""
            seen["issue_notes"] = issue_notes
            return corrected

        with patch.object(
            representation.short_planning_repair,
            "_repair_existing_moment",
            side_effect=transport,
        ) as repair:
            result = representation._repair_short_representation_once(
                broken,
                ["inner_dialogue_missing_visible_exchange"],
                args=("key", broken.topic, "moment", "gemini-2.5-flash"),
                kwargs={"research_context": {"approved_research_pack": []}},
            )

        repair.assert_called_once()
        self.assertEqual(seen["stage_id"], "planning.short_repair")
        self.assertIn("two short inner-thought turns", seen["issue_notes"])
        self.assertEqual(result.narrative_format, "short_inner_dialogue")
        self.assertEqual(result.editorial_intent["short_template"], "inner_dialogue")

    def test_long_form_is_not_routed_into_short_representation_repair(self) -> None:
        long_plan = self._long()
        calls = {"n": 0}

        def repair(plan, issues):
            calls["n"] += 1
            return plan

        resolved = representation.resolve_short_representation_for_handoff(
            long_plan,
            research_context={"approved_research_pack": []},
            repair_fn=repair,
        )
        self.assertIs(resolved, long_plan)
        self.assertEqual(calls["n"], 0)
        self.assertEqual(representation.short_representation_issues(long_plan), [])
        self.assertEqual(
            representation.authoritative_section_text(long_plan, long_plan.sections[0]),
            "هذه هي الجملة المسموعة في الطويل.",
        )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

from isco_video_agent.models import ProductionPlan, ScriptSection

from scripts import production_text_representation_contract as representation
from scripts import short_planning_repair as repair


class Run190ShortRepairInvariantCarryForwardTests(unittest.TestCase):
    TOPIC = "لحظة صغيرة تغيّر طريقة فهم الموقف"

    def _short(self, template: str, visible: str) -> ProductionPlan:
        return ProductionPlan(
            topic=self.TOPIC,
            pillar="understand",
            format="moment",
            hook="قد تتغير قراءة الموقف في ثانية واحدة.",
            title_options=["لحظة مختلفة", "ما الذي تغيّر؟", "قبل الحكم"],
            thumbnail_concepts=["quiet doorway", "window reflection", "empty chair"],
            sections=[
                ScriptSection(
                    id="s1",
                    narration="",
                    visual_query="person closing a door quietly portrait realistic",
                    on_screen_text=visible,
                    emotion="reflective",
                    expected_seconds=15.0,
                    key_point="المعنى يظهر من التحول نفسه.",
                )
            ],
            cta="",
            closing_payoff="المعنى يظهر من التحول نفسه.",
            narrative_format=f"short_{template}",
            editorial_intent={"short_template": template},
        )

    def _valid_micro_story(self) -> ProductionPlan:
        return self._short(
            "micro_story",
            "حين أُغلق الباب بهدوء، تغيّر إيقاع اللحظة وظهر معنى مختلف.",
        )

    def test_unrelated_dossier_repair_carries_live_micro_story_postcondition(self) -> None:
        plan = self._valid_micro_story()
        prompt = repair.build_short_repair_prompt(
            plan,
            "Independent tone review requested a surgical wording correction.",
            research_context={"approved_research_pack": []},
        )

        self.assertIn("LIVE SHORT REPRESENTATION POSTCONDITION", prompt)
        self.assertIn("DETERMINISTIC MICRO_STORY ACCEPTANCE", prompt)
        self.assertIn(representation._MICRO_STORY_SCENE_MARKERS[0], prompt)
        self.assertIn(representation._MICRO_STORY_TURN_MARKERS[0], prompt)
        self.assertLessEqual(
            len(prompt.encode("utf-8")),
            repair.SHORT_REPAIR_PROMPT_MAX_BYTES,
        )

    def test_live_validator_marker_change_propagates_to_unrelated_repair(self) -> None:
        plan = self._valid_micro_story()
        with mock.patch.object(
            representation,
            "_MICRO_STORY_SCENE_MARKERS",
            ("مشهدتجريبي",),
        ), mock.patch.object(
            representation,
            "_MICRO_STORY_TURN_MARKERS",
            ("تحولتجريبي",),
        ):
            prompt = repair.build_short_repair_prompt(
                plan,
                "Unrelated Dossier correction.",
                research_context={},
            )

        self.assertIn("مشهدتجريبي", prompt)
        self.assertIn("تحولتجريبي", prompt)

    def test_all_four_templates_carry_their_live_representation_guidance(self) -> None:
        visible_by_template = {
            "inner_dialogue": "— «أحتاج جوابًا الآن». — «قد أحتاج أن أفهم الفكرة أولًا».",
            "why_reframe": "لكن الفكرة الأولى ليست الحكم الأخير؛ قد يظهر معنى أدق بعدها.",
            "micro_story": "حين أُغلق الباب بهدوء، تغيّر إيقاع اللحظة وظهر معنى مختلف.",
            "quote_reflection": "«لست مضطرًا إلى حسم كل شيء الآن»؛ العبارة تترك مساحة للفهم.",
        }
        issue_by_template = dict(repair._TEMPLATE_REPRESENTATION_ISSUES)
        for template, visible in visible_by_template.items():
            with self.subTest(template=template):
                prompt = repair.build_short_repair_prompt(
                    self._short(template, visible),
                    "Unrelated Dossier correction.",
                    research_context={},
                )
                live = representation._REPRESENTATION_REPAIR_GUIDANCE[
                    issue_by_template[template]
                ]
                self.assertIn(live, prompt)

    def test_regressing_dossier_candidate_is_rejected_before_handoff(self) -> None:
        current = self._valid_micro_story()
        regressed = self._short(
            "micro_story",
            "فكرة مجردة بلا مشهد أو تحول واضح.",
        )

        with mock.patch.object(
            repair.native_short,
            "json_text",
            return_value={"shape": "mocked"},
        ) as routed, mock.patch.object(
            repair.native_short,
            "_plan_from_dict",
            return_value=regressed,
        ), mock.patch.object(
            repair.native_short,
            "load_editorial_policy",
            return_value={},
        ):
            with self.assertRaisesRegex(
                repair.ShortRepairEnvelopeError,
                "violated live representation postcondition:micro_story_missing_concrete_event_progression",
            ):
                repair._repair_existing_moment(
                    current,
                    "Independent tone review requested a surgical correction.",
                    api_key="k",
                    topic=current.topic,
                    requested_format="moment",
                    content_model="model",
                    research_context={},
                )

        routed.assert_called_once()

    def test_valid_dossier_candidate_passes_once_and_preserves_template_metadata(self) -> None:
        current = self._valid_micro_story()
        corrected = self._valid_micro_story()
        corrected.sections[0].key_point = "صياغة أدق مع بقاء المشهد والتحول واضحين."
        corrected.editorial_intent = {}
        corrected.narrative_format = ""

        output = io.StringIO()
        with mock.patch.object(
            repair.native_short,
            "json_text",
            return_value={"shape": "mocked"},
        ) as routed, mock.patch.object(
            repair.native_short,
            "_plan_from_dict",
            return_value=corrected,
        ), mock.patch.object(
            repair.native_short,
            "load_editorial_policy",
            return_value={},
        ), redirect_stdout(output):
            result = repair._repair_existing_moment(
                current,
                "Independent tone review requested a surgical correction.",
                api_key="k",
                topic=current.topic,
                requested_format="moment",
                content_model="model",
                research_context={},
            )

        routed.assert_called_once()
        self.assertIs(result, corrected)
        self.assertEqual(result.narrative_format, "short_micro_story")
        self.assertEqual(result.editorial_intent["short_template"], "micro_story")
        self.assertEqual(representation.short_representation_issues(result), [])
        self.assertIn("representation_postcondition=micro_story", output.getvalue())


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

from isco_video_agent.models import ProductionPlan, ScriptSection

from scripts import production_text_representation_contract as representation
from scripts import short_planning_repair as repair


class Run189MicroStoryContractAlignmentTests(unittest.TestCase):
    ISSUE = "micro_story_missing_concrete_event_progression"

    def _short(self, visible: str) -> ProductionPlan:
        return ProductionPlan(
            topic="لحظة صغيرة تغيّر طريقة فهم الموقف",
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
                    key_point="قصة قصيرة لها مشهد وتحول واضحان.",
                )
            ],
            cta="",
            closing_payoff="المعنى يظهر من التحول نفسه.",
            narrative_format="short_micro_story",
            editorial_intent={"short_template": "micro_story"},
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
                    narration="هذه هي الجملة المسموعة في الحلقة الطويلة.",
                    visual_query="quiet room wide shot",
                    on_screen_text="بطاقة مختصرة",
                    expected_seconds=30.0,
                    key_point="فكرة مستقلة",
                )
            ],
            cta="",
            closing_payoff="خلاصة طويلة",
            narrative_format="direct_cinematic",
        )

    def test_repair_prompt_compiles_acceptance_examples_from_live_validator(self) -> None:
        broken = self._short("فكرة مجردة بلا مشهد أو حدث.")
        prompt = repair.build_short_repair_prompt(
            broken,
            self.ISSUE,
            research_context={"approved_research_pack": []},
        )

        self.assertIn("DETERMINISTIC MICRO_STORY ACCEPTANCE", prompt)
        self.assertIn(representation._MICRO_STORY_SCENE_MARKERS[0], prompt)
        self.assertIn(representation._MICRO_STORY_TURN_MARKERS[0], prompt)
        self.assertLessEqual(
            len(prompt.encode("utf-8")),
            repair.SHORT_REPAIR_PROMPT_MAX_BYTES,
        )

    def test_validator_marker_change_automatically_changes_repair_prompt(self) -> None:
        broken = self._short("فكرة مجردة بلا مشهد أو حدث.")
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
                broken,
                self.ISSUE,
                research_context={},
            )

        self.assertIn("مشهدتجريبي", prompt)
        self.assertIn("تحولتجريبي", prompt)

    def test_run189_family_repair_is_one_call_then_validator_pass(self) -> None:
        broken = self._short("فكرة مجردة بلا مشهد أو حدث.")
        corrected = self._short(
            "حين أُغلق الباب بهدوء، تغيّر إيقاع اللحظة وظهر معنى مختلف."
        )
        sent: dict[str, str] = {}

        def provider(api_key, prompt, model):
            sent["prompt"] = prompt
            return {"shape": "mocked"}

        output = io.StringIO()
        with mock.patch.object(
            repair.native_short,
            "json_text",
            side_effect=provider,
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
                broken,
                self.ISSUE,
                api_key="k",
                topic=broken.topic,
                requested_format="moment",
                content_model="model",
                research_context={"approved_research_pack": []},
            )

        self.assertIs(result, corrected)
        routed.assert_called_once()
        self.assertIn("DETERMINISTIC MICRO_STORY ACCEPTANCE", sent["prompt"])
        self.assertEqual(representation.short_representation_issues(result), [])

        logs = output.getvalue()
        self.assertIn(
            "phase=before_repair template=micro_story source=deterministic_validator scene_cue=false turn_cue=false",
            logs,
        )
        self.assertIn(
            "phase=after_repair template=micro_story source=deterministic_validator scene_cue=true turn_cue=true",
            logs,
        )
        self.assertNotIn("فكرة مجردة بلا مشهد أو حدث.", logs)
        self.assertNotIn("حين أُغلق الباب", logs)

    def test_non_micro_story_repairs_do_not_receive_micro_story_contract(self) -> None:
        plan = self._short("لكن المعنى الأول ليس الحكم الأخير.")
        plan.narrative_format = "short_why_reframe"
        plan.editorial_intent = {"short_template": "why_reframe"}
        prompt = repair.build_short_repair_prompt(
            plan,
            "why_reframe_missing_explicit_contrast_or_reframe",
            research_context={},
        )
        self.assertNotIn("DETERMINISTIC MICRO_STORY ACCEPTANCE", prompt)

    def test_long_representation_stays_narration_authoritative_and_unaffected(self) -> None:
        long_plan = self._long()
        self.assertEqual(
            representation.authoritative_section_text(long_plan, long_plan.sections[0]),
            "هذه هي الجملة المسموعة في الحلقة الطويلة.",
        )
        self.assertEqual(representation.short_representation_issues(long_plan), [])
        self.assertEqual(
            repair._representation_contract_guidance("long_form_duplicate_key_points"),
            "",
        )


if __name__ == "__main__":
    unittest.main()

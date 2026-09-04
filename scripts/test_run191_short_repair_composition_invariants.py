from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

from isco_video_agent.models import ProductionPlan, ScriptSection

from scripts import production_text_representation_contract as representation
from scripts import short_planning_repair as repair


class Run191ShortRepairCompositionInvariantTests(unittest.TestCase):
    def _short(self, template: str, visible: str) -> ProductionPlan:
        return ProductionPlan(
            topic="لحظة صغيرة تغيّر قراءة الموقف",
            pillar="understand",
            format="moment",
            hook="قد يتغير معنى الموقف في لحظة.",
            title_options=["لحظة مختلفة", "قبل الحكم", "ما الذي تغيّر؟"],
            thumbnail_concepts=["quiet doorway", "window reflection", "empty chair"],
            sections=[
                ScriptSection(
                    id="s1",
                    narration="",
                    visual_query="quiet doorway portrait realistic",
                    on_screen_text=visible,
                    emotion="reflective",
                    expected_seconds=15.0,
                    key_point="فكرة واحدة واضحة.",
                )
            ],
            cta="",
            closing_payoff="المعنى يظهر من التحول نفسه.",
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
                    narration="هذه جملة مسموعة في الحلقة الطويلة.",
                    visual_query="quiet room wide shot",
                    on_screen_text="بطاقة مختصرة",
                    emotion="reflective",
                    expected_seconds=30.0,
                    key_point="فكرة مستقلة",
                )
            ],
            cta="",
            closing_payoff="خلاصة طويلة",
            narrative_format="direct_cinematic",
        )

    def test_unrelated_dossier_repair_still_receives_live_micro_story_invariant(self) -> None:
        source = self._short(
            "micro_story",
            "حين أُغلق الباب بهدوء، تغيّر إيقاع اللحظة وظهر معنى مختلف.",
        )
        prompt = repair.build_short_repair_prompt(
            source,
            "moment_story_beats_not_distinct",
            research_context={},
        )

        self.assertIn("DETERMINISTIC SHORT MUTATION INVARIANT", prompt)
        self.assertIn("PRESERVE selected template=micro_story", prompt)
        self.assertIn("DETERMINISTIC MICRO_STORY ACCEPTANCE", prompt)
        self.assertIn(representation._MICRO_STORY_SCENE_MARKERS[0], prompt)
        self.assertIn(representation._MICRO_STORY_TURN_MARKERS[0], prompt)
        self.assertLessEqual(
            len(prompt.encode("utf-8")),
            repair.SHORT_REPAIR_PROMPT_MAX_BYTES,
        )

    def test_run191_regressed_second_repair_restores_certified_representation_without_third_call(self) -> None:
        source = self._short(
            "micro_story",
            "حين أُغلق الباب بهدوء، تغيّر إيقاع اللحظة وظهر معنى مختلف.",
        )
        regressed = self._short(
            "micro_story",
            "فكرة مجردة عن معنى الموقف من دون مشهد أو تحول ظاهر.",
        )
        self.assertEqual(representation.short_representation_issues(source), [])
        self.assertEqual(
            representation.short_representation_issues(regressed),
            ["micro_story_missing_concrete_event_progression"],
        )

        output = io.StringIO()
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
        ), redirect_stdout(output):
            result = repair._repair_existing_moment(
                source,
                "moment_story_beats_not_distinct",
                api_key="k",
                topic=source.topic,
                requested_format="moment",
                content_model="model",
                research_context={},
            )

        routed.assert_called_once()
        self.assertEqual(representation.short_representation_issues(result), [])
        self.assertEqual(
            result.sections[0].on_screen_text,
            source.sections[0].on_screen_text,
        )
        self.assertEqual(result.editorial_intent, source.editorial_intent)
        self.assertEqual(result.narrative_format, source.narrative_format)
        logs = output.getvalue()
        self.assertIn("phase=before_repair", logs)
        self.assertIn("scene_cue=true turn_cue=true", logs)
        self.assertIn("candidate_regressed=true", logs)
        self.assertIn("action=restore_representation_owned_fields restored=true", logs)
        self.assertNotIn(source.sections[0].on_screen_text, logs)

    def test_representation_owned_repair_can_replace_invalid_source_instead_of_rolling_it_back(self) -> None:
        source = self._short("micro_story", "فكرة مجردة بلا مشهد أو تحول.")
        corrected = self._short(
            "micro_story",
            "عندما فتح النافذة، تغيّر صمت الغرفة وظهر المعنى.",
        )
        self.assertTrue(representation.short_representation_issues(source))
        self.assertEqual(representation.short_representation_issues(corrected), [])

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
        ):
            result = repair._repair_existing_moment(
                source,
                "micro_story_missing_concrete_event_progression",
                api_key="k",
                topic=source.topic,
                requested_format="moment",
                content_model="model",
                research_context={},
            )

        routed.assert_called_once()
        self.assertEqual(representation.short_representation_issues(result), [])
        self.assertEqual(result.sections[0].on_screen_text, corrected.sections[0].on_screen_text)

    def test_invariant_rollback_is_template_family_wide_not_micro_story_only(self) -> None:
        source = self._short(
            "why_reframe",
            "المشكلة ليست في بطء الخطوة، بل في الحكم عليها قبل أن تكتمل.",
        )
        regressed = self._short(
            "why_reframe",
            "هذه مجرد فكرة ثابتة لا تحمل إعادة صياغة واضحة.",
        )
        self.assertEqual(representation.short_representation_issues(source), [])
        self.assertTrue(representation.short_representation_issues(regressed))

        restored = repair._restore_certified_representation_if_regressed(source, regressed)
        self.assertEqual(representation.short_representation_issues(restored), [])
        self.assertEqual(restored.sections[0].on_screen_text, source.sections[0].on_screen_text)

    def test_long_form_gets_no_short_mutation_contract(self) -> None:
        long_plan = self._long()
        self.assertEqual(
            repair._representation_contract_guidance(
                "long_form_duplicate_key_points",
                long_plan,
            ),
            "",
        )
        self.assertEqual(representation.short_representation_issues(long_plan), [])


if __name__ == "__main__":
    unittest.main()

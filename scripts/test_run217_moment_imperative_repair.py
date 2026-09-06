from __future__ import annotations

from contextlib import nullcontext
import unittest
from unittest.mock import patch

from isco_video_agent.models import ProductionPlan, ScriptSection

from scripts import producer_planning_lifecycle as lifecycle
from scripts import producer_short_repair_guidance
from scripts.producer_quality_contract import ProducerQualityContractError, plan_quality_issues
from scripts.production_failure_diagnostics import classify_production_failure


class Run217MomentImperativeRepairTests(unittest.TestCase):
    def _plan(
        self,
        *,
        hook: str,
        on_screen: str = "لكن الهدوء قد يكشف معنى مختلفًا للموقف",
        closing: str = "أحيانًا يبدأ التغيير عندما ترى الموقف من زاوية أخرى",
        cta: str = "جرب أن تراقب الفكرة بهدوء.",
    ) -> ProductionPlan:
        return ProductionPlan(
            topic="لماذا يزداد الضغط حين تحاول إصلاح كل شيء فورًا؟",
            pillar="understand",
            format="moment",
            hook=hook,
            title_options=["حين يصبح الإصلاح ضغطًا", "مساحة قبل الرد", "زاوية أخرى"],
            thumbnail_concepts=["quiet room", "window light", "still hands"],
            sections=[
                ScriptSection(
                    id="s1",
                    narration="",
                    visual_query="person sitting quietly by window portrait realistic",
                    on_screen_text=on_screen,
                    emotion="reflective",
                    expected_seconds=15.0,
                    key_point="الاستعجال في إصلاح كل شيء قد يزيد الضغط بدل أن يحله",
                )
            ],
            cta=cta,
            closing_payoff=closing,
            narrative_format="short_why_reframe",
            editorial_intent={"short_template": "why_reframe"},
        )

    def test_gate_owned_target_detection_covers_story_fields_and_excludes_cta(self) -> None:
        plan = self._plan(
            hook="توقف عن إصلاح كل شيء فورًا",
            on_screen="جرب أن تمنح الموقف لحظة قبل الرد",
            closing="تذكر أن الهدوء قد يغير زاوية الرؤية",
            cta="ابدأ الآن بخطوة صغيرة.",
        )

        self.assertEqual(
            producer_short_repair_guidance.moment_direct_imperative_targets(plan),
            ["hook", "sections[0].on_screen_text", "closing_payoff"],
        )
        self.assertIn("moment_direct_imperative_in_story_beat", plan_quality_issues(plan))

    def test_run217_repair_prompt_receives_deterministic_failing_field_paths(self) -> None:
        broken = self._plan(hook="توقف عن إصلاح كل شيء فورًا")
        corrected = self._plan(hook="أحيانًا يتحول إصلاح كل شيء فورًا إلى ضغط إضافي")
        captured: dict[str, str] = {}

        def repair(plan, issue_notes, **kwargs):
            self.assertIs(plan, broken)
            captured["issue_notes"] = issue_notes
            return corrected

        with (
            patch.object(
                lifecycle.planning_repair_identity_family,
                "short_repair_stage_spec",
                return_value=object(),
            ),
            patch.object(
                lifecycle.planning_stage_contract,
                "request_stage_scope",
                return_value=nullcontext(),
            ),
            patch.object(
                lifecycle.short_planning_repair,
                "_repair_existing_moment",
                side_effect=repair,
            ),
        ):
            result = lifecycle._repair_short_plan_once(
                broken,
                ["moment_direct_imperative_in_story_beat"],
                args=("key", broken.topic, "moment", "model"),
                kwargs={},
                research_context={"approved_research_pack": []},
            )

        self.assertIs(result, corrected)
        notes = captured["issue_notes"]
        self.assertIn("DETERMINISTIC_ACCEPTANCE_RULE", notes)
        self.assertIn('FAILING_FIELD_PATHS=["hook"]', notes)
        self.assertIn("call_to_action/cta may remain imperative", notes)
        self.assertNotIn("توقف عن إصلاح كل شيء فورًا", notes)

    def test_exhausted_single_repair_reports_safe_field_path_and_stays_fail_closed(self) -> None:
        broken = self._plan(hook="توقف عن إصلاح كل شيء فورًا")

        with self.assertRaisesRegex(
            ProducerQualityContractError,
            r"moment_direct_imperative_in_story_beat:failing_field_paths=hook",
        ):
            lifecycle.resolve_plan_for_producer_handoff(
                broken,
                research_context={"approved_research_pack": []},
                repair_fn=lambda plan, issues: plan,
            )

    def test_producer_quality_error_is_classified_as_planning_not_internal(self) -> None:
        exc = ProducerQualityContractError(
            "producer_plan_handoff_blocked:moment_direct_imperative_in_story_beat"
        )
        self.assertEqual(
            classify_production_failure(exc),
            ("planning", "PRODUCER_PLAN_QUALITY_BLOCK"),
        )


if __name__ == "__main__":
    unittest.main()

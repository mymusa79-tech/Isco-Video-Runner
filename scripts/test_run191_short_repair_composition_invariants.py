from __future__ import annotations

import io
import unittest
from contextlib import nullcontext, redirect_stdout
from types import SimpleNamespace
from unittest import mock

from isco_video_agent.models import ProductionPlan, ScriptSection
from isco_video_agent.planning_operation import planning_operation_scope

from scripts import native_short_stage_contract as short_stage
from scripts import planning_stage_contract as planning
from scripts import producer_planning_lifecycle as lifecycle
from scripts import producer_short_repair_guidance
from scripts import production_text_representation_contract as representation
from scripts import short_planning_repair as repair
from scripts.producer_quality_contract import ProducerQualityContractError, plan_quality_issues
from scripts.production_failure_diagnostics import classify_production_failure


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


class Run217MomentImperativeAcceptanceRegressionTests(unittest.TestCase):
    """F33 continuation: the Producer predicate must compile into the same one repair."""

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

    def test_repair_prompt_receives_deterministic_failing_field_paths(self) -> None:
        broken = self._plan(hook="توقف عن إصلاح كل شيء فورًا")
        corrected = self._plan(hook="أحيانًا يتحول إصلاح كل شيء فورًا إلى ضغط إضافي")
        captured: dict[str, str] = {}

        def repair_call(plan, issue_notes, **kwargs):
            self.assertIs(plan, broken)
            captured["issue_notes"] = issue_notes
            return corrected

        with (
            mock.patch.object(
                lifecycle.planning_repair_identity_family,
                "short_repair_stage_spec",
                return_value=object(),
            ),
            mock.patch.object(
                lifecycle.planning_stage_contract,
                "request_stage_scope",
                return_value=nullcontext(),
            ),
            mock.patch.object(
                lifecycle.short_planning_repair,
                "_repair_existing_moment",
                side_effect=repair_call,
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


class NamedShortStageOwnershipRegressionTests(unittest.TestCase):
    """F33: operation names own identity; order is validation only, never inference."""

    TOPIC = "فخ المجاملة المستمرة: لماذا يصعب عليك قول لا حتى لمن تحب؟"

    def _state(self) -> dict:
        return {"topic": self.TOPIC, "operations": []}

    def test_stage_identity_comes_from_named_engine_operation(self) -> None:
        state = self._state()
        with planning_operation_scope("short_draft"):
            draft = short_stage._stage_for_operation(state)
        with planning_operation_scope("short_review"):
            review = short_stage._stage_for_operation(state)

        self.assertEqual(draft.stage_id, "planning.short_draft")
        self.assertEqual(draft.contract_id, "planning.short_draft.v1")
        self.assertEqual(review.stage_id, "planning.short_review")
        self.assertEqual(review.contract_id, "planning.short_review.v1")
        self.assertEqual(state["operations"], ["short_draft", "short_review"])

    def test_missing_named_operation_fails_closed_before_stage_selection(self) -> None:
        state = self._state()
        with self.assertRaises(planning.PlanningStageError) as raised:
            short_stage._stage_for_operation(state)

        self.assertEqual(
            raised.exception.code,
            planning.PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
        )
        self.assertEqual(raised.exception.stage_id, "planning.short_missing_operation")
        self.assertEqual(state["operations"], [])

    def test_review_cannot_arrive_before_draft_even_when_named(self) -> None:
        state = self._state()
        with planning_operation_scope("short_review"):
            with self.assertRaises(planning.PlanningStageError) as raised:
                short_stage._stage_for_operation(state)

        self.assertEqual(raised.exception.stage_id, "planning.short_operation_sequence")
        self.assertEqual(state["operations"], [])

    def test_duplicate_draft_is_rejected_instead_of_becoming_review(self) -> None:
        state = self._state()
        with planning_operation_scope("short_draft"):
            first = short_stage._stage_for_operation(state)
            with self.assertRaises(planning.PlanningStageError) as raised:
                short_stage._stage_for_operation(state)

        self.assertEqual(first.stage_id, "planning.short_draft")
        self.assertEqual(raised.exception.stage_id, "planning.short_operation_sequence")
        self.assertEqual(state["operations"], ["short_draft"])

    def test_ordinal_identity_state_no_longer_exists(self) -> None:
        self.assertFalse(hasattr(short_stage, "_CALL_STATE"))


class ProducerShortStageOwnershipRegressionTests(unittest.TestCase):
    """Production 33904551137: F33 repair composition must own its Stage Contract."""

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

    def test_producer_repair_enters_explicit_short_repair_stage(self) -> None:
        plan = self._plan()
        corrected = self._plan()
        observed = {}

        def repair_call(*args, **kwargs):
            spec = planning._ACTIVE_STAGE_SPEC.get()
            observed["stage_id"] = None if spec is None else spec.stage_id
            observed["contract_id"] = None if spec is None else spec.contract_id
            observed["topic"] = (
                None if spec is None else spec.semantic_rules.get("approved_topic")
            )
            return corrected

        self.assertIsNone(planning._ACTIVE_STAGE_SPEC.get())
        with mock.patch.object(
            lifecycle.short_planning_repair,
            "_repair_existing_moment",
            side_effect=repair_call,
        ) as routed:
            result = self._call(plan)

        self.assertIs(result, corrected)
        self.assertEqual(observed["stage_id"], "planning.short_repair")
        self.assertEqual(observed["contract_id"], "planning.short_repair.v2")
        self.assertEqual(observed["topic"], self.TOPIC)
        self.assertIsNone(planning._ACTIVE_STAGE_SPEC.get())
        routed.assert_called_once()

    def test_repair_scope_restores_outer_stage_after_success(self) -> None:
        plan = self._plan()
        corrected = self._plan()
        outer = short_stage.moment_stage_spec("short_review", self.TOPIC)
        observed = []

        def repair_call(*args, **kwargs):
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
                side_effect=repair_call,
            ):
                self._call(plan)
            self.assertEqual(
                planning._ACTIVE_STAGE_SPEC.get().stage_id,
                "planning.short_review",
            )

        self.assertEqual(observed, ["planning.short_repair"])
        self.assertIsNone(planning._ACTIVE_STAGE_SPEC.get())

    def test_repair_scope_restores_context_after_transport_failure(self) -> None:
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

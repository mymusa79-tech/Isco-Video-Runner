from __future__ import annotations

import unittest

from isco_video_agent import model_output_schemas
from isco_video_agent.models import ProductionPlan, ScriptSection

from scripts import producer_planning_lifecycle as lifecycle
from scripts import producer_quality_contract as quality
from scripts import producer_short_repair_guidance as guidance
from scripts import security_v1_live_binding as security_v1
from scripts.producer_planning_lifecycle import _REPAIRABLE_SHORT_SECTION_SUFFIXES

# Run #243 (real production log): a Short's single-section visual_query reached
# Production - past every existing Planning-time check - too long (>80 chars) AND
# containing punctuation. The real runtime Security gate (security_v1_live_binding.
# _normalized_stock_query -> isco_video_agent.model_output_schemas.validate_visual_query)
# correctly rejected it, but nothing anywhere caught the resulting ModelOutputSchemaError:
# it crashed the entire production deep inside orchestrator.produce()'s visual search,
# minutes after Planning had already finished (TTS synthesized, telegram progress sent).
#
# The fix predicts the same pass/fail boundary at Planning time and routes a doomed
# visual_query through the existing Producer repair-loop transport - the same mechanism
# moment_narration_likely_exceeds_natural_duration and visual_query_empty already use -
# instead of letting Production discover it late. The Security gate itself
# (security_v1_live_binding.py) is not touched by this change at all.


def _short_plan(visual_query: str) -> ProductionPlan:
    return ProductionPlan(
        topic="موضوع الشورت",
        pillar="understand",
        format="moment",
        hook="خطاف قصير وواضح",
        title_options=["عنوان"],
        thumbnail_concepts=["quiet room"],
        sections=[
            ScriptSection(
                id="s1",
                narration="",
                visual_query=visual_query,
                on_screen_text="نقطة قصيرة",
                emotion="reflective",
                expected_seconds=15.0,
                key_point="نقطة مضغوطة",
            )
        ],
        cta="جرب هذا اليوم.",
        closing_payoff="خلاصة قصيرة",
        narrative_format="short_micro_story",
        editorial_intent={"short_template": "micro_story"},
    )


_SAFE_QUERY = "man walking through forest at golden hour"
# Run #243's exact failure shape: over 80 chars and containing punctuation.
_RUN243_STYLE_QUERY = (
    "a quiet person sitting alone by the window, watching the rain fall slowly outside."
)
_TOO_LONG_BUT_PLAIN_QUERY = "a calm person walking slowly through a quiet forest path during golden hour light"
_NON_ASCII_QUERY = "رجل يمشي في الغابة"


class DetectionTests(unittest.TestCase):
    def test_safe_plain_query_is_not_flagged(self) -> None:
        issues = quality.plan_quality_issues(_short_plan(_SAFE_QUERY), research_context={})
        self.assertNotIn("section_1_visual_query_not_stock_search_safe", issues)

    def test_too_long_with_punctuation_is_flagged_run243_shape(self) -> None:
        self.assertGreater(len(_RUN243_STYLE_QUERY), quality._VISUAL_QUERY_MAX_LENGTH)
        issues = quality.plan_quality_issues(_short_plan(_RUN243_STYLE_QUERY), research_context={})
        self.assertIn("section_1_visual_query_not_stock_search_safe", issues)

    def test_too_long_but_otherwise_plain_is_not_flagged_it_is_auto_shortened(self) -> None:
        # Run #106: the real gate safely auto-shortens this case at runtime. Flagging
        # it here would waste the bounded repair budget on a plan that would actually
        # have succeeded unchanged.
        self.assertGreater(len(_TOO_LONG_BUT_PLAIN_QUERY), quality._VISUAL_QUERY_MAX_LENGTH)
        issues = quality.plan_quality_issues(_short_plan(_TOO_LONG_BUT_PLAIN_QUERY), research_context={})
        self.assertNotIn("section_1_visual_query_not_stock_search_safe", issues)

    def test_non_ascii_query_is_flagged(self) -> None:
        issues = quality.plan_quality_issues(_short_plan(_NON_ASCII_QUERY), research_context={})
        self.assertIn("section_1_visual_query_not_stock_search_safe", issues)

    def test_empty_query_is_only_the_existing_empty_issue_not_this_one(self) -> None:
        issues = quality.plan_quality_issues(_short_plan(""), research_context={})
        self.assertIn("section_1_visual_query_empty", issues)
        self.assertNotIn("section_1_visual_query_not_stock_search_safe", issues)


class GuidanceTests(unittest.TestCase):
    def test_guidance_names_the_right_section_and_constraints(self) -> None:
        text = quality.visual_query_stock_search_repair_guidance(1)
        self.assertIn("sections[0].visual_query", text)
        self.assertIn("plain English stock-footage search terms", text)
        self.assertIn("no punctuation", text)
        self.assertIn(str(quality._VISUAL_QUERY_MAX_LENGTH), text)

    def test_combines_with_other_short_repair_guidance(self) -> None:
        plan = _short_plan(_RUN243_STYLE_QUERY)
        combined = guidance.short_producer_repair_guidance(
            plan,
            ["moment_direct_imperative_in_story_beat", "section_1_visual_query_not_stock_search_safe"],
        )
        self.assertIn("moment_direct_imperative_in_story_beat", combined)
        self.assertIn("sections[0].visual_query", combined)

    def test_only_visual_query_issue_present_yields_only_its_guidance(self) -> None:
        plan = _short_plan(_RUN243_STYLE_QUERY)
        text = guidance.short_producer_repair_guidance(
            plan, ["section_1_visual_query_not_stock_search_safe"]
        )
        self.assertIn("sections[0].visual_query", text)
        self.assertNotIn("moment_direct_imperative_in_story_beat", text)


class EndToEndRepairTests(unittest.TestCase):
    def test_run243_style_query_is_fixed_deterministically_without_any_repair_call(self) -> None:
        # Real production evidence (req-c39f532991c1): the exact same issue class as
        # Run #243 (punctuation, over the guidance length) spent its one bounded AI
        # repair call and the repair output was STILL rejected, failing the whole
        # production closed with zero output. sanitize_visual_query_for_stock_search
        # fixes this class of case (stray punctuation, doubled separators) for free -
        # repair_fn must never even be consulted for it.
        broken = _short_plan(_RUN243_STYLE_QUERY)
        repair_calls: list[list[str]] = []

        def repair(plan, issues):
            repair_calls.append(issues)
            return _short_plan(_SAFE_QUERY)

        resolved = lifecycle.resolve_plan_for_producer_handoff(
            broken, research_context={"approved_research_pack": []}, repair_fn=repair,
        )
        self.assertEqual(repair_calls, [])
        self.assertNotIn(
            "section_1_visual_query_not_stock_search_safe",
            quality.plan_quality_issues(resolved, research_context={}),
        )
        # The sanitizer only strips separators; it must not invent or drop words.
        self.assertEqual(
            resolved.sections[0].visual_query,
            "a quiet person sitting alone by the window watching the rain fall slowly outside",
        )

    def test_doomed_visual_query_is_repaired_and_accepted(self) -> None:
        # Non-ASCII text has nothing ASCII-alphanumeric for the deterministic
        # sanitizer to salvage, so this genuinely needs the bounded AI repair call -
        # unlike the punctuation-only case above.
        broken = _short_plan(_NON_ASCII_QUERY)

        def repair(plan, issues):
            self.assertIn("section_1_visual_query_not_stock_search_safe", issues)
            return _short_plan(_SAFE_QUERY)

        resolved = lifecycle.resolve_plan_for_producer_handoff(
            broken, research_context={"approved_research_pack": []}, repair_fn=repair,
        )
        self.assertEqual(resolved.sections[0].visual_query, _SAFE_QUERY)
        self.assertNotIn(
            "section_1_visual_query_not_stock_search_safe",
            quality.plan_quality_issues(resolved, research_context={}),
        )

    def test_repair_that_is_still_unsafe_fails_closed_not_silently_accepted(self) -> None:
        broken = _short_plan(_NON_ASCII_QUERY)

        def repair(plan, issues):
            return _short_plan(_NON_ASCII_QUERY)

        with self.assertRaises(quality.ProducerQualityContractError):
            lifecycle.resolve_plan_for_producer_handoff(
                broken, research_context={"approved_research_pack": []}, repair_fn=repair,
            )

    def test_repair_output_with_leftover_punctuation_is_cleaned_up_not_failed(self) -> None:
        # The AI repair call itself gets the same free deterministic pass: a repair
        # that fixes the content but leaves a trailing period must not fail closed
        # over formatting alone.
        broken = _short_plan(_NON_ASCII_QUERY)

        def repair(plan, issues):
            return _short_plan(_SAFE_QUERY + ".")

        resolved = lifecycle.resolve_plan_for_producer_handoff(
            broken, research_context={"approved_research_pack": []}, repair_fn=repair,
        )
        self.assertEqual(resolved.sections[0].visual_query, _SAFE_QUERY)

    def test_long_format_without_a_repair_path_fails_closed_at_planning_not_deep_in_production(self) -> None:
        # No Long AI-repair path exists for this issue (no real evidence of the Run
        # #243 failure mode needing content rewrites on Long yet), so a genuinely
        # unfixable query must still fail *here*, at Planning handoff, with a clear
        # ProducerQualityContractError - not silently pass through to crash deep
        # inside orchestrator.produce() minutes later. The deterministic sanitizer
        # applies to every format, so use non-ASCII text here too: nothing it can fix.
        long_plan = ProductionPlan(
            topic="موضوع",
            pillar="understand",
            format="story",
            hook="خطاف",
            title_options=["أ"],
            thumbnail_concepts=["a"],
            sections=[
                ScriptSection(
                    id="s1",
                    narration="سرد",
                    visual_query=_NON_ASCII_QUERY,
                    on_screen_text="نص",
                    emotion="reflective",
                    expected_seconds=20.0,
                    key_point="نقطة",
                )
            ],
            cta="جرب",
            closing_payoff="خلاصة",
            narrative_format="direct_cinematic",
            editorial_intent={},
        )
        with self.assertRaises(quality.ProducerQualityContractError) as captured:
            lifecycle.resolve_plan_for_producer_handoff(
                long_plan, research_context={"approved_research_pack": []},
            )
        self.assertIn("section_1_visual_query_not_stock_search_safe", str(captured.exception))

    def test_long_format_run243_style_query_is_also_fixed_deterministically(self) -> None:
        # The sanitizer is format-agnostic and strictly safe (same accepted shape or
        # empty), so Long gets the same free fix Short does for pure formatting noise,
        # even though Long has no AI repair path for this issue at all.
        long_plan = ProductionPlan(
            topic="موضوع",
            pillar="understand",
            format="story",
            hook="خطاف",
            title_options=["أ"],
            thumbnail_concepts=["a"],
            sections=[
                ScriptSection(
                    id="s1",
                    narration="سرد",
                    visual_query=_RUN243_STYLE_QUERY,
                    on_screen_text="نص",
                    emotion="reflective",
                    expected_seconds=20.0,
                    key_point="نقطة",
                )
            ],
            cta="جرب",
            closing_payoff="خلاصة",
            narrative_format="direct_cinematic",
            editorial_intent={},
        )
        resolved = lifecycle.resolve_plan_for_producer_handoff(
            long_plan, research_context={"approved_research_pack": []},
        )
        self.assertNotIn(
            "section_1_visual_query_not_stock_search_safe",
            quality.plan_quality_issues(resolved, research_context={}),
        )


class SanitizeVisualQueryTests(unittest.TestCase):
    def test_already_safe_query_is_unchanged(self) -> None:
        self.assertEqual(quality.sanitize_visual_query_for_stock_search(_SAFE_QUERY), _SAFE_QUERY)

    def test_run243_style_punctuation_and_doubled_separators_are_stripped(self) -> None:
        sanitized = quality.sanitize_visual_query_for_stock_search(_RUN243_STYLE_QUERY)
        self.assertTrue(quality.visual_query_survives_stock_search_gate(sanitized))
        self.assertEqual(
            sanitized,
            "a quiet person sitting alone by the window watching the rain fall slowly outside",
        )

    def test_non_ascii_query_sanitizes_to_empty_it_cannot_be_salvaged(self) -> None:
        self.assertEqual(quality.sanitize_visual_query_for_stock_search(_NON_ASCII_QUERY), "")

    def test_empty_input_sanitizes_to_empty(self) -> None:
        self.assertEqual(quality.sanitize_visual_query_for_stock_search(""), "")

    def test_result_never_exceeds_the_cross_provider_length_ceiling(self) -> None:
        overlong = " ".join(["word"] * 100)
        sanitized = quality.sanitize_visual_query_for_stock_search(overlong)
        self.assertLessEqual(len(sanitized), quality._VISUAL_QUERY_CROSS_PROVIDER_TEXT_MAX_LENGTH)
        self.assertTrue(quality.visual_query_survives_stock_search_gate(sanitized))

    def test_output_always_either_empty_or_passes_the_gate(self) -> None:
        for value in (
            _SAFE_QUERY,
            _RUN243_STYLE_QUERY,
            _NON_ASCII_QUERY,
            "a" * 300,
            "!!!???...",
            "café scene, softly-lit",
            "",
        ):
            with self.subTest(value=value):
                sanitized = quality.sanitize_visual_query_for_stock_search(value)
                self.assertTrue(sanitized == "" or quality.visual_query_survives_stock_search_gate(sanitized))


class RepairableIssueRegistrationTests(unittest.TestCase):
    def test_suffix_is_registered_in_the_existing_repair_transport(self) -> None:
        self.assertIn("_visual_query_not_stock_search_safe", _REPAIRABLE_SHORT_SECTION_SUFFIXES)


class VisualQueryStockSearchGateParityTests(unittest.TestCase):
    """The predictor must never disagree with the real runtime Security gate on whether
    Production would actually crash. This is the drift guard: it calls the exact same
    function Production calls (security_v1_live_binding._normalized_stock_query), not a
    reimplementation, so a future change to the real gate's boundary fails this test
    loudly instead of the two silently disagreeing."""

    _CASES = (
        _SAFE_QUERY,
        _RUN243_STYLE_QUERY,
        _TOO_LONG_BUT_PLAIN_QUERY,
        _NON_ASCII_QUERY,
        "a" * 300,
        "",
        "simple query",
        "café scene",
    )

    def _real_gate_accepts(self, value: str) -> bool:
        try:
            security_v1._normalized_stock_query(value)
            return True
        except model_output_schemas.ModelOutputSchemaError:
            return False

    def test_predictor_agrees_with_the_real_gate_on_every_case(self) -> None:
        for value in self._CASES:
            with self.subTest(value=value):
                self.assertEqual(
                    quality.visual_query_survives_stock_search_gate(value),
                    self._real_gate_accepts(value),
                )

    def test_run243_case_actually_raises_on_the_real_gate(self) -> None:
        with self.assertRaises(model_output_schemas.ModelOutputSchemaError):
            security_v1._normalized_stock_query(_RUN243_STYLE_QUERY)

    def test_length_ceiling_constants_match_the_real_engine_module(self) -> None:
        self.assertEqual(quality._VISUAL_QUERY_MAX_LENGTH, model_output_schemas.VISUAL_QUERY_MAX_LENGTH)
        self.assertEqual(
            quality._VISUAL_QUERY_CROSS_PROVIDER_TEXT_MAX_LENGTH,
            model_output_schemas.CROSS_PROVIDER_TEXT_MAX_LENGTH,
        )


if __name__ == "__main__":
    unittest.main()

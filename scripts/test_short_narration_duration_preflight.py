from __future__ import annotations

import unittest

import isco_video_agent.orchestrator as orchestrator
from isco_video_agent.models import ProductionPlan, ScriptSection

from scripts import producer_planning_lifecycle as lifecycle
from scripts import producer_quality_contract as quality
from scripts import producer_short_repair_guidance as guidance
from scripts import short_editorial_craft_contract as craft
from scripts import short_voice_v2
from scripts.producer_planning_lifecycle import _REPAIRABLE_SHORT_PRODUCER_ISSUES

# This suite closes a real, code-verified gap in the live Short production path
# (Voice-Owned Timeline, which replaced short_voice_v2.py's speed-fitting approach the
# day after Run #196): build_voice_owned_timeline() raises VOICE_TIMELINE_EXCEEDS_
# SHORT_MAX with zero retry when the natural narration is too long for the Short
# duration ceiling, and nothing anywhere caught that signal to actually repair it -
# confirmed by tracing the call chain to run_control_production.py. The fix catches an
# over-long narration during Planning, before any real TTS call, and routes it through
# the existing Producer repair-loop transport (the same one moment_story_beats_not_
# distinct etc. already use) instead of inventing a new mechanism.
#
# The one hard requirement this suite must prove and keep proving: the fix must NEVER
# touch how voice is spoken. Voice-Owned Timeline's whole reason to exist is that
# Run #196 proved speed-compressing natural narration is unsafe - post_speed_factor
# stays 1.0 and time_compression stays False everywhere. This preflight only asks
# Planning to write a more concise script; it must never suggest faster delivery,
# clipped pauses, or any post-recording compression.

def _short_plan(*, template: str, hook: str, on_screen: str, title: str, closing: str) -> ProductionPlan:
    return ProductionPlan(
        topic="موضوع الشورت",
        pillar="understand",
        format="moment",
        hook=hook,
        title_options=[title],
        thumbnail_concepts=["quiet room"],
        sections=[
            ScriptSection(
                id="s1",
                narration="",
                visual_query="a calm portrait scene realistic",
                on_screen_text=on_screen,
                emotion="reflective",
                expected_seconds=15.0,
                key_point="نقطة مضغوطة",
            )
        ],
        cta="جرب هذا اليوم.",
        closing_payoff=closing,
        narrative_format=f"short_{template}",
        editorial_intent={"short_template": template},
    )


_SHORT_BEAT = "جملة قصيرة"
_LONG_BEAT = " ".join(["كلمة"] * 40)  # ~40 words alone comfortably exceeds the ceiling


class DurationEstimateDetectionTests(unittest.TestCase):
    def test_voice_led_long_narration_is_flagged(self) -> None:
        plan = _short_plan(
            template="micro_story",  # voice_led: every beat is spoken
            hook=_LONG_BEAT, on_screen=_LONG_BEAT, title=_LONG_BEAT, closing=_LONG_BEAT,
        )
        issues = quality.plan_quality_issues(plan, research_context={})
        self.assertIn("moment_narration_likely_exceeds_natural_duration", issues)

    def test_voice_led_normal_narration_is_not_flagged(self) -> None:
        plan = _short_plan(
            template="micro_story",
            hook=_SHORT_BEAT, on_screen=_SHORT_BEAT + " ثاني", title=_SHORT_BEAT + " ثالث",
            closing=_SHORT_BEAT + " رابع",
        )
        issues = quality.plan_quality_issues(plan, research_context={})
        self.assertNotIn("moment_narration_likely_exceeds_natural_duration", issues)

    def test_hybrid_ignores_a_long_middle_beat_that_is_never_spoken(self) -> None:
        # why_reframe is hybrid: only hook + closing_payoff are ever spoken. A long
        # title/on_screen_text (visual-only, hybrid's middle beats) must not trip this
        # check - flagging it would send the repair after the wrong fields entirely.
        plan = _short_plan(
            template="why_reframe",
            hook=_SHORT_BEAT, on_screen=_LONG_BEAT, title=_LONG_BEAT, closing=_SHORT_BEAT,
        )
        issues = quality.plan_quality_issues(plan, research_context={})
        self.assertNotIn("moment_narration_likely_exceeds_natural_duration", issues)

    def test_hybrid_flags_a_long_hook_or_payoff_which_are_spoken(self) -> None:
        plan = _short_plan(
            template="why_reframe",
            hook=_LONG_BEAT, on_screen=_SHORT_BEAT, title=_SHORT_BEAT, closing=_LONG_BEAT,
        )
        issues = quality.plan_quality_issues(plan, research_context={})
        self.assertIn("moment_narration_likely_exceeds_natural_duration", issues)


class RepairGuidanceNeverTouchesDeliveryTests(unittest.TestCase):
    def test_guidance_never_instructs_faster_or_compressed_delivery(self) -> None:
        # "compress"/"speed" legitimately appear once, inside the reassurance that
        # nothing downstream ever does that - so this checks no *instruction* to speed
        # up or clip pauses exists, not a blind substring ban that would also flag that
        # reassurance sentence itself.
        plan = _short_plan(
            template="micro_story",
            hook=_LONG_BEAT, on_screen=_LONG_BEAT, title=_LONG_BEAT, closing=_LONG_BEAT,
        )
        text = quality.short_narration_duration_guidance(plan, "micro_story")
        self.assertTrue(text)
        for term in ("faster speech", "clipped pauses", "speak faster", "increase speed", "atempo"):
            self.assertNotIn(term, text)
        self.assertIn(
            "nothing downstream ever speeds up or time-compresses narration", text
        )

    def test_guidance_leads_with_narrowing_the_idea_before_any_word_count(self) -> None:
        # The philosophy fix this test locks in: "narrow the idea, keep it complete and
        # natural" must be the PRIMARY instruction, with the word count appearing only
        # afterward as a subordinate, explicitly-non-strict aid - never the other way
        # around, which would just move Run #196's failure from audio to writing.
        plan = _short_plan(
            template="micro_story",
            hook=_LONG_BEAT, on_screen=_LONG_BEAT, title=_LONG_BEAT, closing=_LONG_BEAT,
        )
        text = quality.short_narration_duration_guidance(plan, "micro_story")
        narrow_index = text.index("narrowing the IDEA")
        word_count_index = text.index("words tends to fit naturally")
        self.assertLess(narrow_index, word_count_index)
        self.assertIn("rough guide only", text)
        self.assertIn("not a strict target to hit at the cost of a rushed or truncated result", text)

    def test_guidance_forbids_mechanical_word_deletion_and_clause_chaining(self) -> None:
        plan = _short_plan(
            template="micro_story",
            hook=_LONG_BEAT, on_screen=_LONG_BEAT, title=_LONG_BEAT, closing=_LONG_BEAT,
        )
        text = quality.short_narration_duration_guidance(plan, "micro_story")
        self.assertIn("Do not simply delete words from the existing sentences", text)
        self.assertIn("do not chain the remaining clauses back together", text)
        self.assertIn(
            "keep the idea complete and pick an even narrower framing of the topic "
            "rather than forcing artificial brevity",
            text,
        )

    def test_hybrid_guidance_only_names_the_spoken_fields(self) -> None:
        plan = _short_plan(
            template="why_reframe",
            hook=_LONG_BEAT, on_screen=_SHORT_BEAT, title=_SHORT_BEAT, closing=_LONG_BEAT,
        )
        text = quality.short_narration_duration_guidance(plan, "why_reframe")
        self.assertIn("hook", text)
        self.assertIn("closing_payoff", text)
        self.assertNotIn("on_screen_text", text)
        self.assertNotIn("title_options", text)

    def test_voice_led_guidance_names_every_spoken_field(self) -> None:
        plan = _short_plan(
            template="micro_story",
            hook=_LONG_BEAT, on_screen=_LONG_BEAT, title=_LONG_BEAT, closing=_LONG_BEAT,
        )
        text = quality.short_narration_duration_guidance(plan, "micro_story")
        for path in ("hook", "title_options[0]", "on_screen_text", "closing_payoff"):
            self.assertIn(path, text)

    def test_short_producer_repair_guidance_combines_with_other_issues(self) -> None:
        # Existing single-issue behavior (imperative-only) must be untouched by
        # supporting more than one issue's guidance in the same call.
        plan = _short_plan(
            template="micro_story",
            hook="قل لا بثقة", on_screen=_SHORT_BEAT, title=_SHORT_BEAT, closing=_SHORT_BEAT,
        )
        only_imperative = guidance.short_producer_repair_guidance(
            plan, ["moment_direct_imperative_in_story_beat"]
        )
        self.assertIn("moment_direct_imperative_in_story_beat", only_imperative)
        self.assertNotIn("moment_narration_likely_exceeds_natural_duration", only_imperative)

        long_plan = _short_plan(
            template="micro_story",
            hook="قل لا بثقة", on_screen=_LONG_BEAT, title=_LONG_BEAT, closing=_LONG_BEAT,
        )
        combined = guidance.short_producer_repair_guidance(
            long_plan,
            ["moment_direct_imperative_in_story_beat", "moment_narration_likely_exceeds_natural_duration"],
        )
        self.assertIn("moment_direct_imperative_in_story_beat", combined)
        self.assertIn("moment_narration_likely_exceeds_natural_duration", combined)


class EndToEndRepairPhilosophyTests(unittest.TestCase):
    """The mechanical duration check cannot itself tell a verbosely-written simple idea
    apart from a genuinely rich one that deserves a narrower angle rather than a cut -
    that distinction is inherently a semantic judgment the repair call makes, guided by
    short_narration_duration_guidance()'s philosophy (proven above). What this class
    proves instead is that the existing bounded repair-loop wiring correctly accepts
    EITHER a proper outcome (safely trimmed, or narrowed-but-complete) and correctly
    still fails closed if a repair attempt produces neither - i.e. the mechanism itself
    does not silently accept a truncated or still-too-long result just because a repair
    was attempted."""

    def test_verbose_simple_idea_is_safely_trimmed_and_accepted(self) -> None:
        # Case (ب): the underlying idea is simple - a wordier retelling and a tight one
        # carry the same point, so trimming loses nothing.
        verbose = _short_plan(
            template="micro_story",
            hook=(
                "في الحقيقة وبكل صراحة وبكل بساطة إن الأمر كله ببساطة شديدة هو أنك "
                "تحتاج فقط أن تبدأ بخطوة واحدة صغيرة جدا كل يوم دون أن تنتظر الكمال أبدا"
            ),
            on_screen=_SHORT_BEAT, title=_SHORT_BEAT,
            closing=(
                "ولهذا فإن الخلاصة النهائية في نهاية الأمر لكل ما سبق ذكره هي ببساطة "
                "أن البداية الصغيرة تكون كافية تماما في أغلب الأحيان"
            ),
        )

        def repair(plan, issues):
            self.assertIn("moment_narration_likely_exceeds_natural_duration", issues)
            return _short_plan(
                template="micro_story",
                hook="خطوة صغيرة كل يوم تكفي", on_screen=_SHORT_BEAT, title=_SHORT_BEAT,
                closing="البداية الصغيرة كافية",
            )

        resolved = lifecycle.resolve_plan_for_producer_handoff(
            verbose, research_context={"approved_research_pack": []}, repair_fn=repair,
        )
        self.assertEqual(resolved.hook, "خطوة صغيرة كل يوم تكفي")
        self.assertNotIn(
            "moment_narration_likely_exceeds_natural_duration",
            quality.plan_quality_issues(resolved, research_context={}),
        )

    def test_rich_idea_reframed_to_a_narrower_angle_is_accepted_not_truncated(self) -> None:
        # Case (أ) done right: the original idea is rich enough that no honest full
        # retelling fits, so the repair picks ONE narrower, still-complete angle instead
        # of chopping the original sentences mid-thought.
        rich = _short_plan(template="micro_story", hook=_LONG_BEAT, on_screen=_LONG_BEAT, title=_LONG_BEAT, closing=_LONG_BEAT)

        def repair(plan, issues):
            self.assertIn("moment_narration_likely_exceeds_natural_duration", issues)
            # A complete, self-contained narrower angle - not a fragment of the
            # original long beats with words simply removed from the middle.
            return _short_plan(
                template="micro_story",
                hook="أحيانا نتردد لأننا ننتظر اللحظة المثالية",
                on_screen=_SHORT_BEAT, title=_SHORT_BEAT,
                closing="لكن اللحظة المثالية لا تأتي إلا بعد أن نبدأ",
            )

        resolved = lifecycle.resolve_plan_for_producer_handoff(
            rich, research_context={"approved_research_pack": []}, repair_fn=repair,
        )
        self.assertEqual(resolved.hook, "أحيانا نتردد لأننا ننتظر اللحظة المثالية")
        self.assertNotIn(
            "moment_narration_likely_exceeds_natural_duration",
            quality.plan_quality_issues(resolved, research_context={}),
        )

    def test_repair_that_is_still_too_long_fails_closed_not_silently_accepted(self) -> None:
        rich = _short_plan(template="micro_story", hook=_LONG_BEAT, on_screen=_LONG_BEAT, title=_LONG_BEAT, closing=_LONG_BEAT)

        def repair(plan, issues):
            # A repair attempt that did not actually shorten anything meaningfully.
            return _short_plan(
                template="micro_story", hook=_LONG_BEAT, on_screen=_SHORT_BEAT,
                title=_SHORT_BEAT, closing=_LONG_BEAT,
            )

        with self.assertRaises(quality.ProducerQualityContractError):
            lifecycle.resolve_plan_for_producer_handoff(
                rich, research_context={"approved_research_pack": []}, repair_fn=repair,
            )


class RepairableIssueRegistrationTests(unittest.TestCase):
    def test_issue_is_registered_in_the_existing_repair_transport(self) -> None:
        self.assertIn(
            "moment_narration_likely_exceeds_natural_duration",
            _REPAIRABLE_SHORT_PRODUCER_ISSUES,
        )


class VoiceModeSingleSourceOfTruthTests(unittest.TestCase):
    def test_short_voice_v2_delegates_to_craft_contract_for_every_template(self) -> None:
        for template in craft.template_names():
            with self.subTest(template=template):
                self.assertEqual(
                    short_voice_v2.decide_voice_mode(template),
                    craft.template_voice_mode(template),
                )

    def test_unsupported_template_still_raises_the_historical_runtime_error(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "unsupported template"):
            short_voice_v2.decide_voice_mode("unknown")


class EngineCeilingConsistencyTests(unittest.TestCase):
    def test_local_ceiling_constant_matches_the_real_engine_function(self) -> None:
        # This check runs at Planning time, before quality-final.json exists, so it
        # cannot read Engine's real ceiling at runtime - it duplicates the number
        # instead. This test is the drift guard: if Engine's moment ceiling ever
        # changes, this fails loudly instead of the two silently disagreeing.
        cfg = {"formats": {"moment": {"target_seconds": 15}}}
        minimum, maximum = orchestrator._duration_limits(cfg, "moment")
        self.assertEqual((minimum, maximum), (7.0, 25.0))
        self.assertEqual(maximum, quality._SHORT_NATURAL_DURATION_MAX_SECONDS)


if __name__ == "__main__":
    unittest.main()

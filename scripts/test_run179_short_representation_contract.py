from __future__ import annotations

import unittest

from isco_video_agent.models import ProductionPlan, ScriptSection

from scripts.production_text_representation_contract import (
    augment_sensitive_review_text,
    authoritative_section_text,
    normalize_tone_audit_for_plan,
    screen_religious_quote_marker,
    semantic_repetition_audit_for_plan,
    short_representation_issues,
)


class Run179ShortRepresentationContractTests(unittest.TestCase):
    def _run179_plan(self) -> ProductionPlan:
        return ProductionPlan(
            topic="هوس الإنتاجية: لماذا نشعر بالذنب عندما نرتاح؟",
            pillar="understand",
            format="moment",
            hook="هل كل دقيقة من يومك يجب أن تُقاس بإنجاز؟",
            title_options=["حين تتحول الراحة إلى اختبار", "ماذا لو لم يكن يومك مشروعًا؟", "لا شيء مطلوب منك الآن"],
            thumbnail_concepts=["quiet laptop", "warm sofa", "empty calendar"],
            sections=[
                ScriptSection(
                    id="s1",
                    narration="",
                    visual_query="tired person closing a laptop by a window portrait realistic",
                    on_screen_text="— «عليّ أن أنجز شيئًا مثاليًا الليلة». — … «نسيتُ أنني هنا لأتخفف من التقييم، لا لأحوله إلى مشروع».",
                    emotion="intimate reflective",
                    expected_seconds=15.0,
                    key_point="محادثة داخلية تحوّل لحظة الراحة من مهمة مثالية إلى مساحة بلا تقييم.",
                )
            ],
            cta="",
            closing_payoff="أحيانًا، أكثر ما نحتاجه من الراحة هو ألا نطالبها بأن تثبت شيئًا.",
            narrative_format="short_inner_dialogue",
            editorial_intent={"short_template": "inner_dialogue"},
        )

    def _long_plan(self) -> ProductionPlan:
        return ProductionPlan(
            topic="موضوع طويل",
            pillar="understand",
            format="film",
            hook="خطاف",
            title_options=["أ", "ب", "ج"],
            thumbnail_concepts=["x", "y", "z"],
            sections=[
                ScriptSection(id="s1", narration="هذه هي الجملة المسموعة.", visual_query="desk", on_screen_text="بطاقة قصيرة", key_point="فكرة"),
                ScriptSection(id="s2", narration="وهذه فكرة أخرى.", visual_query="window", on_screen_text="بطاقة ثانية", key_point="فكرة ثانية"),
            ],
            cta="",
            closing_payoff="خلاصة",
            narrative_format="direct_cinematic",
        )

    def test_run179_inner_dialogue_is_valid_viewer_facing_structure(self) -> None:
        plan = self._run179_plan()
        self.assertEqual(short_representation_issues(plan), [])
        self.assertIn("عليّ أن أنجز", authoritative_section_text(plan, plan.sections[0]))
        self.assertEqual(plan.sections[0].narration, "")

    def test_invalid_inner_dialogue_is_not_excused(self) -> None:
        plan = self._run179_plan()
        plan.sections[0].on_screen_text = "الراحة ليست مشروعًا جديدًا."
        self.assertIn(
            "inner_dialogue_missing_visible_exchange",
            short_representation_issues(plan),
        )

    def test_run179_tone_false_positive_is_normalized_only_for_format_flag(self) -> None:
        plan = self._run179_plan()
        raw = {
            "status": "block",
            "preachiness_flags": [],
            "cultural_dignity_flags": [],
            "naturalness_flags": [],
            "narrative_format_flags": ["short_inner_dialogue appears to be a monologue"],
            "unverified_religious_quote_flags": [],
            "notes": [],
            "provider": "groq",
        }
        result = normalize_tone_audit_for_plan(plan, raw)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["narrative_format_flags"], [])
        self.assertEqual(
            result["narrative_format_authority"],
            "runner_deterministic_short_contract",
        )
        self.assertEqual(
            result["ignored_out_of_scope_narrative_format_flags"],
            raw["narrative_format_flags"],
        )

    def test_real_tone_or_safety_flag_stays_blocked(self) -> None:
        plan = self._run179_plan()
        raw = {
            "status": "block",
            "preachiness_flags": [],
            "cultural_dignity_flags": [],
            "naturalness_flags": ["unnatural Arabic cadence"],
            "narrative_format_flags": ["short_inner_dialogue appears to be a monologue"],
            "unverified_religious_quote_flags": [],
            "notes": [],
        }
        result = normalize_tone_audit_for_plan(plan, raw)
        self.assertEqual(result["status"], "block")
        self.assertEqual(result["naturalness_flags"], ["unnatural Arabic cadence"])
        self.assertEqual(result["narrative_format_flags"], raw["narrative_format_flags"])

    def test_invalid_short_representation_stays_blocked(self) -> None:
        plan = self._run179_plan()
        plan.sections[0].on_screen_text = "الراحة ليست مشروعًا جديدًا."
        raw = {
            "status": "block",
            "preachiness_flags": [],
            "cultural_dignity_flags": [],
            "naturalness_flags": [],
            "narrative_format_flags": ["inner dialogue not actually expressed"],
            "unverified_religious_quote_flags": [],
            "notes": [],
        }
        result = normalize_tone_audit_for_plan(plan, raw)
        self.assertEqual(result["status"], "block")
        self.assertIn(
            "inner_dialogue_missing_visible_exchange",
            result["short_representation_issues"],
        )

    def test_single_section_moment_skips_inapplicable_semantic_repetition_provider(self) -> None:
        calls = []

        def original(*args, **kwargs):
            calls.append((args, kwargs))
            return {"status": "block"}

        result = semantic_repetition_audit_for_plan(
            self._run179_plan(), original, "key", self._run179_plan(), "model"
        )
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["reason"], "not_applicable_single_section_moment")
        self.assertEqual(calls, [])

    def test_long_semantic_repetition_path_is_unchanged(self) -> None:
        sentinel = {"status": "pass", "duplicate_groups": [], "reason": "provider_checked"}
        calls = []

        def original(*args, **kwargs):
            calls.append((args, kwargs))
            return sentinel

        plan = self._long_plan()
        result = semantic_repetition_audit_for_plan(plan, original, "key", plan, "model")
        self.assertIs(result, sentinel)
        self.assertEqual(len(calls), 1)

    def test_sensitive_review_receives_moment_on_screen_text(self) -> None:
        plan = self._run179_plan()
        plan.sections[0].on_screen_text = "هذه فتوى غير موثقة تظهر على الشاشة"
        augmented = augment_sensitive_review_text(plan, "topic hook payoff")
        self.assertIn("فتوى", augmented)

    def test_religious_quote_marker_cannot_hide_in_moment_screen_text(self) -> None:
        plan = self._run179_plan()
        plan.sections[0].on_screen_text = "قال رسول الله ثم جملة غير موثقة"
        marker = screen_religious_quote_marker(plan, ("قال الله تعالى", "قال رسول الله"))
        self.assertEqual(marker, "قال رسول الله")

    def test_long_authoritative_text_remains_narration(self) -> None:
        plan = self._long_plan()
        self.assertEqual(
            authoritative_section_text(plan, plan.sections[0]),
            "هذه هي الجملة المسموعة.",
        )
        raw = {
            "status": "block",
            "preachiness_flags": [],
            "cultural_dignity_flags": [],
            "naturalness_flags": [],
            "narrative_format_flags": ["long format issue"],
            "unverified_religious_quote_flags": [],
            "notes": [],
        }
        self.assertEqual(normalize_tone_audit_for_plan(plan, raw), raw)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
from unittest.mock import patch

from isco_video_agent.models import ProductionPlan, ScriptSection

from scripts import tone_audit_representation_bridge as bridge


class Run191ToneAuditRepresentationBridgeTests(unittest.TestCase):
    def _moment(self, visible: str) -> ProductionPlan:
        return ProductionPlan(
            topic="عادة المساء التي تسرق نومك دون أن تنتبه",
            pillar="see",
            format="moment",
            hook="هل يضيء هاتفك في أواخر الليل عندما تحاول النوم؟",
            title_options=["ضوء قبل النوم", "حين يبقى الهاتف", "هدوء المساء"],
            thumbnail_concepts=["phone glow", "dark room", "nightstand"],
            sections=[
                ScriptSection(
                    id="section1",
                    narration="",
                    visual_query="phone glowing on a nightstand in a dark bedroom portrait realistic",
                    on_screen_text=visible,
                    emotion="reflective",
                    expected_seconds=15.0,
                    key_point="إيقاف الهاتف قد يجعل الجو أكثر هدوءًا",
                )
            ],
            cta="جرّب إيقاف تشغيل هاتفك قبل النوم",
            closing_payoff="نوم هادئ يبدأ بتحكم بسيط",
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
                    narration="هذه جملة مسموعة في الحلقة الطويلة.",
                    visual_query="quiet room wide shot",
                    on_screen_text="بطاقة قصيرة",
                    emotion="reflective",
                    expected_seconds=30.0,
                    key_point="فكرة مستقلة",
                )
            ],
            cta="",
            closing_payoff="خلاصة طويلة",
            narrative_format="direct_cinematic",
        )

    def test_live_run191_shape_projects_screen_text_without_mutating_production_plan(self) -> None:
        visible = "عندما يضيء هاتفك في الظلام يجذب انتباهك، ثم يصبح النوم صعبًا"
        plan = self._moment(visible)

        projected = bridge.project_plan_for_tone_audit(plan)

        self.assertIsNot(projected, plan)
        self.assertEqual(plan.sections[0].narration, "")
        self.assertEqual(plan.sections[0].on_screen_text, visible)
        self.assertEqual(projected.sections[0].narration, visible)
        self.assertEqual(projected.sections[0].on_screen_text, visible)
        self.assertEqual(
            getattr(projected, "_isco_tone_audit_representation", ""),
            "moment_on_screen_text",
        )

    def test_long_is_exact_passthrough(self) -> None:
        plan = self._long()
        self.assertIs(bridge.project_plan_for_tone_audit(plan), plan)

    def test_invalid_moment_fails_closed_before_tone_provider(self) -> None:
        broken = self._moment("فكرة مجردة بلا مشهد أو تحول.")
        with self.assertRaisesRegex(
            bridge.ToneAuditRepresentationBridgeError,
            "micro_story_missing_concrete_event_progression",
        ):
            bridge.project_plan_for_tone_audit(broken)

    def test_installed_bridge_changes_input_only_and_preserves_real_block_verdict(self) -> None:
        plan = self._moment(
            "عندما يضيء هاتفك في الظلام يجذب انتباهك، ثم يصبح النوم صعبًا"
        )
        captured: dict[str, object] = {}
        real_block = {
            "status": "block",
            "preachiness_flags": ["genuine preachiness"],
            "cultural_dignity_flags": [],
            "naturalness_flags": [],
            "narrative_format_flags": [],
            "unverified_religious_quote_flags": [],
            "notes": [],
        }

        def fake_audit(api_key: str, audit_plan: object, model: str):
            captured["plan"] = audit_plan
            return real_block

        with patch.object(bridge.orchestrator, "audit_tone_and_naturalness", fake_audit):
            bridge.install_tone_audit_representation_bridge()
            result = bridge.orchestrator.audit_tone_and_naturalness("k", plan, "model")

        projected = captured["plan"]
        self.assertIsNot(projected, plan)
        self.assertEqual(getattr(projected, "sections")[0].narration, plan.sections[0].on_screen_text)
        self.assertIs(result, real_block)
        self.assertEqual(result["status"], "block")
        self.assertEqual(result["preachiness_flags"], ["genuine preachiness"])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest

from isco_video_agent.models import ProductionPlan, ScriptSection

from scripts.production_text_representation_contract import short_representation_issues


class Run180ShortRepresentationMetadataAuthorityTests(unittest.TestCase):
    def _plan(self, *, template: str, topic: str, visible: str) -> ProductionPlan:
        return ProductionPlan(
            topic=topic,
            pillar="understand",
            format="moment",
            hook="خطاف محايد لا يحمل إشارة القالب المطلوبة.",
            title_options=["أ", "ب", "ج"],
            thumbnail_concepts=["x", "y", "z"],
            sections=[
                ScriptSection(
                    id="s1",
                    narration="",
                    visual_query="quiet realistic portrait scene",
                    on_screen_text=visible,
                    emotion="reflective",
                    expected_seconds=15.0,
                    key_point="فكرة",
                )
            ],
            cta="",
            closing_payoff="خلاصة محايدة.",
            narrative_format=f"short_{template}",
            editorial_intent={"short_template": template},
        )

    def test_quote_in_approved_topic_cannot_fake_visible_quote(self) -> None:
        plan = self._plan(
            template="quote_reflection",
            topic="«لا تؤجل حياتك» — تأمل في هذه العبارة",
            visible="تأمل قصير بلا الاقتباس نفسه على الشاشة.",
        )
        self.assertEqual(
            short_representation_issues(plan),
            ["quote_reflection_missing_visible_quote"],
        )

    def test_story_marker_in_topic_cannot_fake_visible_micro_story(self) -> None:
        plan = self._plan(
            template="micro_story",
            topic="عندما تتغير لحظة صغيرة، ماذا نتعلم؟",
            visible="هذه فكرة مجردة بلا حدث ظاهر.",
        )
        self.assertEqual(
            short_representation_issues(plan),
            ["micro_story_missing_concrete_event_progression"],
        )

    def test_reframe_marker_in_topic_cannot_fake_visible_reframe(self) -> None:
        plan = self._plan(
            template="why_reframe",
            topic="المشكلة ليست في البداية بل في فهمها",
            visible="هذه صياغة ثابتة بلا انتقال ظاهر.",
        )
        self.assertEqual(
            short_representation_issues(plan),
            ["why_reframe_missing_explicit_contrast_or_reframe"],
        )


if __name__ == "__main__":
    unittest.main()

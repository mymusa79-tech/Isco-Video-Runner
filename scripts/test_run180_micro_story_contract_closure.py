from __future__ import annotations

import unittest

from isco_video_agent.models import ProductionPlan, ScriptSection

from scripts.production_text_representation_contract import short_representation_issues


class Run180MicroStoryContractClosureTests(unittest.TestCase):
    ISSUE = "micro_story_missing_concrete_event_progression"

    def _plan(self, visible: str) -> ProductionPlan:
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

    def test_scene_without_turn_is_not_a_micro_story_progression(self) -> None:
        plan = self._plan("حين أُغلق الباب بهدوء، بقي كل شيء كما هو.")
        self.assertEqual(short_representation_issues(plan), [self.ISSUE])

    def test_turn_without_scene_is_not_a_micro_story_progression(self) -> None:
        plan = self._plan("تغيّر المعنى فجأة، وصار أوضح من قبل.")
        self.assertEqual(short_representation_issues(plan), [self.ISSUE])

    def test_scene_plus_turn_satisfies_micro_story_progression(self) -> None:
        plan = self._plan("حين أُغلق الباب بهدوء، تغيّر إيقاع اللحظة وظهر معنى مختلف.")
        self.assertEqual(short_representation_issues(plan), [])

    def test_abstract_event_words_do_not_satisfy_contract(self) -> None:
        plan = self._plan("فكرة مجردة بلا مشهد أو حدث.")
        self.assertEqual(short_representation_issues(plan), [self.ISSUE])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import inspect
import unittest
from unittest.mock import patch

import isco_video_agent.resilient_planner as staged

from scripts import planning_stage_contract as stage_contract
from scripts import structural_editorial_contract as structural
from scripts.append_retry_guard import (
    _APPEND_ONLY_DECLARATIVE_CONTRACT,
    _RETRY_ATTEMPTED,
    _apply_guard_additions,
    _repair_all_residual_underlength,
)


class AppendDeclarativeOnlyRun265Tests(unittest.TestCase):
    COUNTS_794 = [110, 110, 110, 110, 90, 88, 88, 88]

    @classmethod
    def _sections(cls) -> list[staged.ScriptSection]:
        sections: list[staged.ScriptSection] = []
        for index, count in enumerate(cls.COUNTS_794, start=1):
            words = [f"كلمة{index}_{word}" for word in range(count)]
            if index <= 3:
                words[-1] += "؟"
            if index == len(cls.COUNTS_794):
                words[-1] = "closer"
            sections.append(
                staged.ScriptSection(
                    id=f"sec_{index}",
                    narration=" ".join(words),
                    visual_query="room notebook",
                    key_point=f"distinct key point {index}",
                )
            )
        return sections

    @classmethod
    def _plan(cls, sections: list[staged.ScriptSection]) -> staged.ProductionPlan:
        return staged.ProductionPlan(
            topic="موضوع تجريبي",
            pillar="understand",
            format="film",
            hook="hook",
            title_options=["a", "b", "c"],
            thumbnail_concepts=["a", "b", "c"],
            sections=sections,
            cta="cta",
            closing_payoff="payoff",
            identity_opener="opener",
            identity_closer="closer",
            identity_transitions=["t1", "t2", "t3"],
            narrative_format="problem_reveal_solution",
        )

    @staticmethod
    def _safe_additions() -> list[dict[str, str]]:
        counts = {"sec_5": 50, "sec_6": 52, "sec_7": 52, "sec_8": 52}
        return [
            {
                "id": section_id,
                "append_text": " ".join(["إضافة"] * words),
            }
            for section_id, words in counts.items()
        ]

    def tearDown(self) -> None:
        _RETRY_ATTEMPTED.set(False)

    def test_prompt_contract_is_declarative_only_and_bound_to_all_append_prompt_variants(self) -> None:
        self.assertIn("declarative prose only", _APPEND_ONLY_DECLARATIVE_CONTRACT)
        self.assertIn('Arabic question mark "؟"', _APPEND_ONLY_DECLARATIVE_CONTRACT)
        self.assertIn('ASCII question mark "?"', _APPEND_ONLY_DECLARATIVE_CONTRACT)
        self.assertIn("Do not create any new rhetorical or interrogative sentence", _APPEND_ONLY_DECLARATIVE_CONTRACT)
        self.assertIn("Leave every question already present in current_narration unchanged", _APPEND_ONLY_DECLARATIVE_CONTRACT)
        self.assertIn("preserve strict append-only semantics", _APPEND_ONLY_DECLARATIVE_CONTRACT)

        source = inspect.getsource(_repair_all_residual_underlength)
        self.assertEqual(source.count("{_APPEND_ONLY_DECLARATIVE_CONTRACT}"), 3)

    def test_real_794_word_shape_crosses_800_without_increasing_three_questions(self) -> None:
        sections = self._sections()
        plan = self._plan(sections)
        before = {section.id: section.narration for section in sections}
        self.assertEqual(sum(staged._word_count(section.narration) for section in sections), 794)
        self.assertEqual(structural._rhetorical_question_count(sections), 3)
        self.assertNotIn(structural._RHETORICAL_QUESTIONS_FLAG, structural._current_flags(sections))

        prompts: list[str] = []
        specs: list[stage_contract.PlanningStageSpec] = []
        original_append_stage_spec = stage_contract.append_stage_spec

        def capture_spec(expected_ids, **kwargs):
            spec = original_append_stage_spec(expected_ids, **kwargs)
            specs.append(spec)
            return spec

        def fake_json(api_key, prompt, model):
            del api_key, model
            prompts.append(prompt)
            return {"additions": self._safe_additions()}

        guarded_owner = structural._with_pre_append_probe(_repair_all_residual_underlength)
        with patch.object(stage_contract, "append_stage_spec", side_effect=capture_spec), patch.object(
            staged, "json_text", side_effect=fake_json
        ):
            additions = guarded_owner(
                "key",
                topic="موضوع تجريبي",
                model="model",
                sections=sections,
                policy_json="{}",
                research_json="{}",
                narrative_format="problem_reveal_solution",
                current_words=794,
                minimum=800,
            )

        self.assertEqual(len(prompts), 1)
        self.assertIn(_APPEND_ONLY_DECLARATIVE_CONTRACT, prompts[0])
        self.assertTrue(all("؟" not in text and "?" not in text for text in additions.values()))
        self.assertEqual(structural._rhetorical_question_count(sections), 3)

        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0].stage_id, "planning.append_only_repair")
        self.assertEqual(specs[0].contract_id, "planning.append_only_repair.candidate.v1")

        _apply_guard_additions(plan, additions)

        self.assertGreaterEqual(staged._plan_word_count(plan), 800)
        self.assertLessEqual(staged._plan_word_count(plan), 1450)
        self.assertEqual(structural._rhetorical_question_count(plan.sections), 3)
        for section in plan.sections:
            words = staged._word_count(section.narration)
            self.assertGreaterEqual(words, 110)
            self.assertLessEqual(words, 170)

        for section in plan.sections[:-1]:
            self.assertTrue(section.narration.startswith(before[section.id]))
        original_closing_prefix = before["sec_8"][: -len("closer")].rstrip()
        self.assertTrue(plan.sections[-1].narration.startswith(original_closing_prefix))
        self.assertTrue(plan.sections[-1].narration.endswith("closer"))
        self.assertEqual(plan.sections[-1].narration.count("closer"), 1)

    def test_existing_655_guard_rejects_provider_question_fail_closed(self) -> None:
        sections = self._sections()
        calls = 0

        def violating_owner(*args, **kwargs):
            nonlocal calls
            del args, kwargs
            calls += 1
            return {"sec_5": "تفصيل جديد يعيد السؤال إلى النص؟"}

        guarded_owner = structural._with_pre_append_probe(violating_owner)
        with self.assertRaisesRegex(
            RuntimeError,
            "append-only guard blocked new rhetorical questions",
        ):
            guarded_owner(
                "key",
                topic="موضوع تجريبي",
                model="model",
                sections=sections,
                policy_json="{}",
                research_json="{}",
                narrative_format="problem_reveal_solution",
                current_words=794,
                minimum=800,
            )

        self.assertEqual(calls, 1)
        self.assertEqual(structural._rhetorical_question_count(sections), 3)


if __name__ == "__main__":
    unittest.main()

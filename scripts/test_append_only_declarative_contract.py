from __future__ import annotations

import unittest
from unittest.mock import patch

import isco_video_agent.resilient_planner as staged

from scripts.append_retry_guard import (
    _APPEND_ONLY_DECLARATIVE_CONTRACT,
    _RETRY_ATTEMPTED,
    _repair_all_residual_underlength,
)
from scripts.structural_editorial_contract import _with_pre_append_probe


class AppendOnlyDeclarativeContractTests(unittest.TestCase):
    @staticmethod
    def _sections_794_with_three_questions() -> list[staged.ScriptSection]:
        sections: list[staged.ScriptSection] = []
        for index in range(1, 8):
            tokens = [f"كلمة{index}"] * 110
            if index <= 3:
                tokens[-1] = f"سؤال{index}؟"
            sections.append(
                staged.ScriptSection(
                    id=f"sec_{index}",
                    narration=" ".join(tokens),
                    visual_query="room notebook",
                    key_point=f"distinct key point {index}",
                )
            )
        sections.append(
            staged.ScriptSection(
                id="sec_8",
                narration=" ".join(["ختام"] * 24),
                visual_query="quiet desk",
                key_point="distinct closing key point",
            )
        )
        return sections

    @staticmethod
    def _question_count(sections: list[staged.ScriptSection]) -> int:
        text = " ".join(section.narration for section in sections)
        return text.count("؟") + text.count("?")

    def tearDown(self) -> None:
        _RETRY_ATTEMPTED.set(False)

    def test_baseline_three_questions_stays_three_and_append_text_is_declarative(self) -> None:
        sections = self._sections_794_with_three_questions()
        self.assertEqual(sum(staged._word_count(s.narration) for s in sections), 794)
        self.assertEqual(self._question_count(sections), 3)
        prompts: list[str] = []
        declarative_append = " ".join(["تفصيل"] * 86)

        def fake_json(api_key, prompt, model):
            del api_key, model
            prompts.append(prompt)
            return {"additions": [{"id": "sec_8", "append_text": declarative_append}]}

        with patch.object(staged, "json_text", side_effect=fake_json):
            additions = _repair_all_residual_underlength(
                "key",
                topic="لماذا تفشل خطط إدارة الوقت في الحياة اليومية",
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
        self.assertNotIn("؟", additions["sec_8"])
        self.assertNotIn("?", additions["sec_8"])
        staged._append_retry_additions(sections, additions)
        self.assertEqual(self._question_count(sections), 3)

    def test_run_shape_794_crosses_800_without_increasing_questions(self) -> None:
        sections = self._sections_794_with_three_questions()
        before = [section.narration for section in sections]
        declarative_append = " ".join(["توضيح"] * 86)

        def fake_json(api_key, prompt, model):
            del api_key, prompt, model
            return {"additions": [{"id": "sec_8", "append_text": declarative_append}]}

        with patch.object(staged, "json_text", side_effect=fake_json):
            additions = _repair_all_residual_underlength(
                "key",
                topic="لماذا تفشل خطط إدارة الوقت في الحياة اليومية",
                model="model",
                sections=sections,
                policy_json="{}",
                research_json="{}",
                narrative_format="problem_reveal_solution",
                current_words=794,
                minimum=800,
            )

        staged._append_retry_additions(sections, additions)
        after_words = sum(staged._word_count(s.narration) for s in sections)
        self.assertGreaterEqual(after_words, 800)
        self.assertEqual(after_words, 880)
        self.assertEqual(self._question_count(sections), 3)
        for index, original in enumerate(before):
            self.assertTrue(sections[index].narration.startswith(original))
        for section in sections:
            self.assertGreaterEqual(staged._word_count(section.narration), 110)
            self.assertLessEqual(staged._word_count(section.narration), 170)
        self.assertLessEqual(after_words, 1450)

    def test_all_existing_append_prompt_paths_include_same_declarative_contract(self) -> None:
        sections = self._sections_794_with_three_questions()
        prompts: list[str] = []
        calls = 0

        def fake_json(api_key, prompt, model):
            nonlocal calls
            del api_key, model
            prompts.append(prompt)
            calls += 1
            if calls == 1:
                return {"additions": []}
            if calls == 2:
                return {
                    "additions": [
                        {"id": "sec_8", "append_text": " ".join(["قصير"] * 10)}
                    ]
                }
            return {
                "additions": [
                    {"id": "sec_8", "append_text": " ".join(["تفصيل"] * 86)}
                ]
            }

        with patch.object(staged, "json_text", side_effect=fake_json):
            additions = _repair_all_residual_underlength(
                "key",
                topic="لماذا تفشل خطط إدارة الوقت في الحياة اليومية",
                model="model",
                sections=sections,
                policy_json="{}",
                research_json="{}",
                narrative_format="problem_reveal_solution",
                current_words=794,
                minimum=800,
            )

        self.assertEqual(calls, 3)
        self.assertEqual(len(prompts), 3)
        for prompt in prompts:
            self.assertIn(_APPEND_ONLY_DECLARATIVE_CONTRACT, prompt)
            self.assertIn('Do not add the Arabic question mark "؟"', prompt)
            self.assertIn('or the ASCII question mark "?"', prompt)
            self.assertIn("Do not create any new rhetorical or interrogative sentence", prompt)
            self.assertIn("never delete, replace, or rewrite any existing narration", prompt)
        self.assertNotIn("؟", additions["sec_8"])
        self.assertNotIn("?", additions["sec_8"])

    def test_noncompliant_provider_is_still_blocked_by_existing_655_guard(self) -> None:
        sections = self._sections_794_with_three_questions()
        existing_owner_calls = 0

        def noncompliant_owner(*args, **kwargs):
            nonlocal existing_owner_calls
            del args, kwargs
            existing_owner_calls += 1
            return {"sec_8": "إضافة تقريرية ثم سؤال جديد؟"}

        guarded_owner = _with_pre_append_probe(noncompliant_owner)
        with self.assertRaisesRegex(
            RuntimeError,
            "Structural Editorial append-only guard blocked new rhetorical questions",
        ):
            guarded_owner(sections=sections)
        self.assertEqual(existing_owner_calls, 1)


if __name__ == "__main__":
    unittest.main()

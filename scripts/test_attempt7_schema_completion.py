from __future__ import annotations

import unittest
from unittest.mock import patch

import isco_video_agent.resilient_planner as staged

from scripts.append_retry_guard import (
    _RETRY_ATTEMPTED,
    _parse_ordered_subset_for_schema_completion,
    _repair_all_residual_underlength,
    _residual_deficit,
)


class Attempt7SchemaCompletionRegressionTests(unittest.TestCase):
    COUNTS = [65, 65, 65, 65, 65, 65, 65, 65]

    @staticmethod
    def _sections(counts: list[int]) -> list[staged.ScriptSection]:
        return [
            staged.ScriptSection(
                id=f"sec_{index}",
                narration=" ".join([f"كلمة{index}"] * words),
                visual_query="room notebook",
                key_point=f"distinct key point {index}",
            )
            for index, words in enumerate(counts, start=1)
        ]

    @staticmethod
    def _addition(section_id: str, words: int = 51) -> dict[str, str]:
        return {
            "id": section_id,
            "append_text": " ".join(["إضافة"] * words),
        }

    def tearDown(self) -> None:
        _RETRY_ATTEMPTED.set(False)

    def test_attempt7_partial_first_response_completes_missing_ids_once_before_apply(self) -> None:
        sections = self._sections(self.COUNTS)
        calls: list[str] = []

        def fake_json(api_key, prompt, model):
            del api_key, model
            calls.append(prompt)
            if len(calls) == 1:
                return {
                    "additions": [
                        self._addition("sec_1"),
                        self._addition("sec_2"),
                        self._addition("sec_3"),
                        self._addition("sec_4"),
                    ]
                }
            return {
                "additions": [
                    self._addition("sec_5"),
                    self._addition("sec_6"),
                    self._addition("sec_7"),
                    self._addition("sec_8"),
                ]
            }

        before = [section.narration for section in sections]
        with patch.object(staged, "json_text", side_effect=fake_json):
            additions = _repair_all_residual_underlength(
                "key",
                topic="صوت الآخرين في رأسك",
                model="model",
                sections=sections,
                policy_json="{}",
                research_json="{}",
                narrative_format="problem_reveal_solution",
                current_words=sum(self.COUNTS),
                minimum=800,
            )

        self.assertEqual(len(calls), 2)
        self.assertIn("ONE bounded target-completion request", calls[1])
        self.assertIn('"id": "sec_5"', calls[1])
        self.assertIn('"id": "sec_8"', calls[1])
        self.assertEqual([section.narration for section in sections], before)
        self.assertEqual(list(additions), [f"sec_{index}" for index in range(1, 9)])

        staged._append_retry_additions(sections, additions)
        self.assertEqual(_residual_deficit(sections), 0)
        for section in sections:
            self.assertGreaterEqual(staged._word_count(section.narration), 110)
            self.assertLessEqual(staged._word_count(section.narration), 170)
        self.assertLessEqual(sum(staged._word_count(s.narration) for s in sections), 1450)

    def test_schema_completion_is_strict_and_never_gets_a_third_call(self) -> None:
        sections = self._sections(self.COUNTS)
        calls = 0

        def fake_json(api_key, prompt, model):
            nonlocal calls
            del api_key, prompt, model
            calls += 1
            if calls == 1:
                return {
                    "additions": [
                        self._addition("sec_1"),
                        self._addition("sec_2"),
                        self._addition("sec_3"),
                        self._addition("sec_4"),
                    ]
                }
            return {
                "additions": [
                    self._addition("sec_5"),
                    self._addition("sec_6"),
                    self._addition("sec_7"),
                ]
            }

        with patch.object(staged, "json_text", side_effect=fake_json):
            with self.assertRaisesRegex(RuntimeError, "exactly 4 additions"):
                _repair_all_residual_underlength(
                    "key",
                    topic="صوت الآخرين في رأسك",
                    model="model",
                    sections=sections,
                    policy_json="{}",
                    research_json="{}",
                    narrative_format="problem_reveal_solution",
                    current_words=sum(self.COUNTS),
                    minimum=800,
                )
        self.assertEqual(calls, 2)

    def test_subset_parser_rejects_unknown_duplicate_and_reordered_ids(self) -> None:
        expected = ["sec_1", "sec_2", "sec_3"]
        with self.assertRaisesRegex(RuntimeError, "unexpected section id"):
            _parse_ordered_subset_for_schema_completion(
                {"additions": [self._addition("sec_9")]}, expected
            )
        with self.assertRaisesRegex(RuntimeError, "duplicated section id"):
            _parse_ordered_subset_for_schema_completion(
                {
                    "additions": [
                        self._addition("sec_1"),
                        self._addition("sec_1"),
                    ]
                },
                expected,
            )
        with self.assertRaisesRegex(RuntimeError, "preserve the required section-id order"):
            _parse_ordered_subset_for_schema_completion(
                {
                    "additions": [
                        self._addition("sec_2"),
                        self._addition("sec_1"),
                    ]
                },
                expected,
            )


if __name__ == "__main__":
    unittest.main()

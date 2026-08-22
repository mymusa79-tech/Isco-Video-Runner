from __future__ import annotations

import unittest
from unittest.mock import patch

import isco_video_agent.resilient_planner as staged
import scripts.append_retry_guard as append_guard
from scripts.attempt10_append_bound_recovery import (
    _recoverable_first_pass_split,
    install_attempt10_append_bound_recovery,
)


class Attempt10AppendBoundRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_split = append_guard._split_held_and_underfloor_additions
        self.retry_token = append_guard._RETRY_ATTEMPTED.set(False)

    def tearDown(self) -> None:
        append_guard._split_held_and_underfloor_additions = self.original_split
        append_guard._RETRY_ATTEMPTED.reset(self.retry_token)

    @staticmethod
    def _spec() -> dict:
        return {
            "id": "sec_8",
            "current_words": 100,
            "hard_section_band": [110, 170],
            "minimum_append_words": 16,
            "maximum_append_words": 35,
        }

    def test_attempt10_exact_69_over_35_is_discarded_for_completion(self) -> None:
        held, retry_ids = _recoverable_first_pass_split(
            {"sec_8": " ".join(["إضافة"] * 69)},
            [self._spec()],
            aggregate_headroom=200,
        )
        self.assertEqual(held, {})
        self.assertEqual(retry_ids, ["sec_8"])

    def test_hard_band_safe_first_pass_addition_is_held(self) -> None:
        text = " ".join(["إضافة"] * 16)
        held, retry_ids = _recoverable_first_pass_split(
            {"sec_8": text},
            [self._spec()],
            aggregate_headroom=200,
        )
        self.assertEqual(held, {"sec_8": text})
        self.assertEqual(retry_ids, [])

    def test_first_pass_oversize_uses_only_existing_completion_call(self) -> None:
        install_attempt10_append_bound_recovery()
        counts = [90] * 8
        sections = [
            staged.ScriptSection(
                id=f"sec_{index}",
                narration=" ".join([f"كلمة{index}"] * words),
                visual_query="room notebook",
                key_point=f"key point {index}",
            )
            for index, words in enumerate(counts, start=1)
        ]
        original = [section.narration for section in sections]
        calls: list[str] = []

        def fake_json(api_key, prompt, model):
            del api_key, model
            calls.append(prompt)
            if len(calls) == 1:
                additions = [
                    {
                        "id": f"sec_{index}",
                        "append_text": " ".join(["إضافة"] * (69 if index == 8 else 26)),
                    }
                    for index in range(1, 9)
                ]
                return {"additions": additions}
            self.assertIn('"sec_8"', prompt)
            return {
                "additions": [
                    {"id": "sec_8", "append_text": " ".join(["إضافة"] * 26)}
                ]
            }

        with patch.object(staged, "json_text", side_effect=fake_json):
            additions = append_guard._repair_all_residual_underlength(
                "key",
                topic="topic",
                model="model",
                sections=sections,
                policy_json="{}",
                research_json="{}",
                narrative_format="problem_reveal_solution",
                current_words=720,
                minimum=800,
            )

        self.assertEqual(len(calls), 2)
        self.assertEqual([section.narration for section in sections], original)
        self.assertEqual(append_guard._word_count(additions["sec_8"]), 26)
        append_guard.staged._append_retry_additions(sections, additions)
        self.assertTrue(all(110 <= append_guard._word_count(s.narration) <= 170 for s in sections))
        self.assertLessEqual(sum(append_guard._word_count(s.narration) for s in sections), 1450)

    def test_completion_response_still_fails_closed_when_oversize(self) -> None:
        install_attempt10_append_bound_recovery()
        sections = [
            staged.ScriptSection(
                id=f"sec_{index}",
                narration=" ".join([f"كلمة{index}"] * 90),
                visual_query="room notebook",
                key_point=f"key point {index}",
            )
            for index in range(1, 9)
        ]
        calls = 0

        def fake_json(api_key, prompt, model):
            nonlocal calls
            del api_key, prompt, model
            calls += 1
            if calls == 1:
                return {
                    "additions": [
                        {
                            "id": f"sec_{index}",
                            "append_text": " ".join(["إضافة"] * (69 if index == 8 else 26)),
                        }
                        for index in range(1, 9)
                    ]
                }
            return {
                "additions": [
                    {"id": "sec_8", "append_text": " ".join(["إضافة"] * 69)}
                ]
            }

        with patch.object(staged, "json_text", side_effect=fake_json):
            with self.assertRaisesRegex(RuntimeError, "maximum allowed"):
                append_guard._repair_all_residual_underlength(
                    "key",
                    topic="topic",
                    model="model",
                    sections=sections,
                    policy_json="{}",
                    research_json="{}",
                    narrative_format="problem_reveal_solution",
                    current_words=720,
                    minimum=800,
                )
        self.assertEqual(calls, 2)


if __name__ == "__main__":
    unittest.main()

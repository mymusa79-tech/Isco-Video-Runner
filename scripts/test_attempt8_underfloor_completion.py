from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import isco_video_agent.resilient_planner as staged

from scripts.append_retry_guard import (
    _RETRY_ATTEMPTED,
    _repair_all_residual_underlength,
    _residual_deficit,
)


class Attempt8UnderfloorCompletionRegressionTests(unittest.TestCase):
    # Synthetic eight-section shape constrained by the literal Attempt 8 trace:
    # aggregate current_words=684, sec_1 current=106 because 7 appended words
    # produced 113, and sec_2 current=96 because 7 appended words produced 103.
    COUNTS = [106, 96, 80, 80, 80, 80, 80, 82]

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

    @classmethod
    def _first_response(cls) -> dict:
        additions = [
            {"id": "sec_1", "append_text": " ".join(["إضافة"] * 7)},
            # Literal failure shape: 96 + 7 = 103, below the hard 110 floor.
            {"id": "sec_2", "append_text": " ".join(["إضافة"] * 7)},
        ]
        additions.extend(
            {"id": f"sec_{index}", "append_text": " ".join(["إضافة"] * 36)}
            for index in range(3, 8)
        )
        additions.append(
            {"id": "sec_8", "append_text": " ".join(["إضافة"] * 34)}
        )
        return {"additions": additions}

    @staticmethod
    def _target_ids_from_completion_prompt(prompt: str) -> list[str]:
        marker = "TARGETS_TO_COMPLETE:\n"
        start = prompt.index(marker) + len(marker)
        end = prompt.index("\n\nALL_SECTION_KEY_POINTS", start)
        specs = json.loads(prompt[start:end])
        return [str(spec["id"]) for spec in specs]

    def tearDown(self) -> None:
        _RETRY_ATTEMPTED.set(False)

    def test_attempt8_underfloor_target_is_replaced_once_before_any_text_is_applied(self) -> None:
        sections = self._sections(self.COUNTS)
        self.assertEqual(sum(staged._word_count(s.narration) for s in sections), 684)
        calls: list[str] = []

        def fake_json(api_key, prompt, model):
            del api_key, model
            calls.append(prompt)
            if len(calls) == 1:
                return self._first_response()
            self.assertEqual(self._target_ids_from_completion_prompt(prompt), ["sec_2"])
            return {
                "additions": [
                    {"id": "sec_2", "append_text": " ".join(["إضافة"] * 20)}
                ]
            }

        with patch.object(staged, "json_text", side_effect=fake_json):
            additions = _repair_all_residual_underlength(
                "key",
                topic="صوت الآخرين في رأسك",
                model="model",
                sections=sections,
                policy_json="{}",
                research_json="{}",
                narrative_format="problem_reveal_solution",
                current_words=684,
                minimum=800,
            )

        self.assertEqual(len(calls), 2)
        self.assertEqual(staged._word_count(additions["sec_1"]), 7)
        self.assertEqual(staged._word_count(additions["sec_2"]), 20)
        staged._append_retry_additions(sections, additions)
        self.assertEqual(_residual_deficit(sections), 0)
        for section in sections:
            self.assertGreaterEqual(staged._word_count(section.narration), 110)
            self.assertLessEqual(staged._word_count(section.narration), 170)
        self.assertLessEqual(
            sum(staged._word_count(section.narration) for section in sections),
            1450,
        )

    def test_underfloor_completion_is_strict_and_has_no_third_call(self) -> None:
        sections = self._sections(self.COUNTS)
        calls = 0

        def fake_json(api_key, prompt, model):
            nonlocal calls
            del api_key, prompt, model
            calls += 1
            if calls == 1:
                return self._first_response()
            return {
                "additions": [
                    {"id": "sec_2", "append_text": " ".join(["إضافة"] * 7)}
                ]
            }

        with patch.object(staged, "json_text", side_effect=fake_json):
            with self.assertRaisesRegex(RuntimeError, "would produce 103 section words"):
                _repair_all_residual_underlength(
                    "key",
                    topic="صوت الآخرين في رأسك",
                    model="model",
                    sections=sections,
                    policy_json="{}",
                    research_json="{}",
                    narrative_format="problem_reveal_solution",
                    current_words=684,
                    minimum=800,
                )
        self.assertEqual(calls, 2)
        # The repair function returns additions; it never mutates narration itself.
        self.assertEqual(
            [staged._word_count(section.narration) for section in sections],
            self.COUNTS,
        )

    def test_post_build_path_above_aggregate_floor_keeps_one_call_fail_closed(self) -> None:
        counts = [121, 119, 109, 106, 108, 99, 110, 81]
        sections = self._sections(counts)
        calls = 0

        def fake_json(api_key, prompt, model):
            nonlocal calls
            del api_key, prompt, model
            calls += 1
            additions = []
            for index, words in enumerate(counts, start=1):
                if words < 110:
                    amount = 110 - words + 6
                    if index == 4:
                        amount = 1
                    additions.append(
                        {
                            "id": f"sec_{index}",
                            "append_text": " ".join(["إضافة"] * amount),
                        }
                    )
            return {"additions": additions}

        with patch.object(staged, "json_text", side_effect=fake_json):
            with self.assertRaisesRegex(RuntimeError, "required final section band is 110-170"):
                _repair_all_residual_underlength(
                    "key",
                    topic="topic",
                    model="model",
                    sections=sections,
                    policy_json="{}",
                    research_json="{}",
                    narrative_format="problem_reveal_solution",
                    current_words=sum(counts),
                    minimum=800,
                )
        self.assertEqual(calls, 1)


if __name__ == "__main__":
    unittest.main()

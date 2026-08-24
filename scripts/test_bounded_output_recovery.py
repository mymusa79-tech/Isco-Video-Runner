from __future__ import annotations

import unittest
from unittest.mock import patch

import isco_video_agent.resilient_planner as staged
import scripts.append_retry_guard as append_guard
from scripts.attempt10_append_bound_recovery import (
    _FIRST_PASS_UNDERFLOOR,
    install_attempt10_append_bound_recovery,
)
from scripts.bounded_output_recovery import (
    _aggregate_tightening_ids,
    _allocate_caps,
    install_bounded_output_recovery,
)


class BoundedOutputRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_repair = append_guard._repair_all_residual_underlength
        self.original_validator = append_guard._validate_addition_bounds
        self.original_split = append_guard._split_held_and_underfloor_additions
        self.original_staged_retry = staged._script_doctor_underlength_retry
        self.carry_token = _FIRST_PASS_UNDERFLOOR.set({})

    def tearDown(self) -> None:
        append_guard._repair_all_residual_underlength = self.original_repair
        append_guard._validate_addition_bounds = self.original_validator
        append_guard._split_held_and_underfloor_additions = self.original_split
        staged._script_doctor_underlength_retry = self.original_staged_retry
        _FIRST_PASS_UNDERFLOOR.reset(self.carry_token)

    @staticmethod
    def _sections(count: int = 30) -> list:
        return [
            staged.ScriptSection(
                id=f"sec_{index}",
                narration=" ".join([f"كلمة{index}"] * count),
                visual_query="room notebook",
                key_point=f"key point {index}",
            )
            for index in range(1, 9)
        ]

    def _install(self) -> None:
        install_attempt10_append_bound_recovery()
        install_bounded_output_recovery()

    def test_run101_present_underfloor_then_short_completion_gets_one_targeted_reask(self) -> None:
        self._install()
        sections = self._sections(30)
        calls: list[str] = []

        def fake_json(api_key, prompt, model):
            del api_key, model
            calls.append(prompt)
            if len(calls) == 1:
                return {
                    "additions": [
                        {
                            "id": f"sec_{index}",
                            "append_text": " ".join(["إضافة"] * (79 if index == 2 else 80)),
                        }
                        for index in range(1, 9)
                    ]
                }
            if len(calls) == 2:
                self.assertIn('"sec_2"', prompt)
                return {
                    "additions": [
                        {"id": "sec_2", "append_text": " ".join(["تكملة"] * 68)}
                    ]
                }
            self.assertEqual(len(calls), 3)
            self.assertIn("ONE FINAL bounded model-output recovery request", prompt)
            self.assertIn('"sec_2"', prompt)
            return {
                "additions": [
                    {"id": "sec_2", "append_text": " ".join(["إنقاذ"] * 82)}
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
                current_words=240,
                minimum=800,
            )

        self.assertEqual(len(calls), 3)
        self.assertEqual(append_guard._word_count(additions["sec_2"]), 82)
        append_guard.staged._append_retry_additions(sections, additions)
        self.assertTrue(
            all(110 <= append_guard._word_count(section.narration) <= 170 for section in sections)
        )
        self.assertLessEqual(
            sum(append_guard._word_count(section.narration) for section in sections),
            1450,
        )

    def test_final_semantic_reask_is_spent_at_most_once(self) -> None:
        self._install()
        sections = self._sections(30)
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
                            "append_text": " ".join(["إضافة"] * (79 if index == 2 else 80)),
                        }
                        for index in range(1, 9)
                    ]
                }
            if calls == 2:
                return {
                    "additions": [
                        {"id": "sec_2", "append_text": " ".join(["تكملة"] * 68)}
                    ]
                }
            self.assertEqual(calls, 3)
            return {
                "additions": [
                    {"id": "sec_2", "append_text": " ".join(["مازال"] * 68)}
                ]
            }

        with patch.object(staged, "json_text", side_effect=fake_json):
            with self.assertRaisesRegex(RuntimeError, "required final section band is 110-170"):
                append_guard._repair_all_residual_underlength(
                    "key",
                    topic="topic",
                    model="model",
                    sections=sections,
                    policy_json="{}",
                    research_json="{}",
                    narrative_format="problem_reveal_solution",
                    current_words=240,
                    minimum=800,
                )
        self.assertEqual(calls, 3)

    def test_unsalvageable_overmax_completion_gets_targeted_reask(self) -> None:
        self._install()
        sections = self._sections(90)
        calls = 0

        def fake_json(api_key, prompt, model):
            nonlocal calls
            del api_key, model
            calls += 1
            if calls == 1:
                return {
                    "additions": [
                        {
                            "id": f"sec_{index}",
                            "append_text": " ".join(["إضافة"] * (69 if index == 2 else 20)),
                        }
                        for index in range(1, 9)
                    ]
                }
            if calls == 2:
                return {
                    "additions": [
                        {"id": "sec_2", "append_text": " ".join(["تكملة"] * 80)}
                    ]
                }
            self.assertIn("over_max_append", prompt)
            return {
                "additions": [
                    {"id": "sec_2", "append_text": " ".join(["إنقاذ"] * 20)}
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
        self.assertEqual(calls, 3)
        self.assertEqual(append_guard._word_count(additions["sec_2"]), 20)

    def test_structural_invalidity_gets_exactly_one_full_residual_reask(self) -> None:
        self._install()
        sections = self._sections(30)
        calls = 0

        def fake_json(api_key, prompt, model):
            nonlocal calls
            del api_key, model
            calls += 1
            if calls == 1:
                return {
                    "additions": [
                        {"id": "sec_1", "append_text": " ".join(["إضافة"] * 80)},
                        {"id": "sec_1", "append_text": " ".join(["مكرر"] * 80)},
                    ]
                }
            self.assertEqual(calls, 2)
            self.assertIn("structure_invalid", prompt)
            return {
                "additions": [
                    {
                        "id": f"sec_{index}",
                        "append_text": " ".join(["إنقاذ"] * 80),
                    }
                    for index in range(1, 9)
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
                current_words=240,
                minimum=800,
            )
        self.assertEqual(calls, 2)
        self.assertEqual(list(additions), [f"sec_{index}" for index in range(1, 9)])

    def test_provider_failure_is_not_replayed_by_output_recovery(self) -> None:
        self._install()
        sections = self._sections(30)
        calls = 0

        def fake_json(api_key, prompt, model):
            nonlocal calls
            del api_key, prompt, model
            calls += 1
            raise RuntimeError("All free providers failed for planning subtask: gemini:HTTP 429")

        with patch.object(staged, "json_text", side_effect=fake_json):
            with self.assertRaisesRegex(RuntimeError, "All free providers failed"):
                append_guard._repair_all_residual_underlength(
                    "key",
                    topic="topic",
                    model="model",
                    sections=sections,
                    policy_json="{}",
                    research_json="{}",
                    narrative_format="problem_reveal_solution",
                    current_words=240,
                    minimum=800,
                )
        self.assertEqual(calls, 1)

    def test_aggregate_tightening_selects_only_reducible_targets(self) -> None:
        additions = {
            "a": " ".join(["x"] * 30),
            "b": " ".join(["x"] * 20),
        }
        specs = [
            {
                "id": "a",
                "current_words": 90,
                "hard_section_band": [110, 170],
                "minimum_append_words": 20,
                "maximum_append_words": 60,
            },
            {
                "id": "b",
                "current_words": 90,
                "hard_section_band": [110, 170],
                "minimum_append_words": 20,
                "maximum_append_words": 60,
            },
        ]
        ids, caps = _aggregate_tightening_ids(additions, specs, overflow=7)
        self.assertEqual(ids, ["a"])
        self.assertEqual(caps["a"], 23)

    def test_cap_allocator_fails_when_aggregate_headroom_cannot_cover_floors(self) -> None:
        specs = [
            {
                "id": "a",
                "current_words": 90,
                "hard_section_band": [110, 170],
                "minimum_append_words": 20,
                "maximum_append_words": 60,
            },
            {
                "id": "b",
                "current_words": 90,
                "hard_section_band": [110, 170],
                "minimum_append_words": 20,
                "maximum_append_words": 60,
            },
        ]
        with self.assertRaisesRegex(RuntimeError, "minimums exceed remaining aggregate headroom"):
            _allocate_caps(
                specs,
                ["a", "b"],
                additions={"a": "", "b": ""},
                aggregate_headroom=39,
            )


if __name__ == "__main__":
    unittest.main()

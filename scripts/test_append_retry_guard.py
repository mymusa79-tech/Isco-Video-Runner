from __future__ import annotations

import unittest
from unittest.mock import patch

import isco_video_agent.resilient_planner as staged

from scripts.append_retry_guard import (
    _FIRST_RETRY_BEFORE_DEFICIT,
    _RETRY_ATTEMPTED,
    _RETRY_ATTEMPTS,
    _SECOND_CHANCE_USED,
    _parse_safe_partial_additions,
    _repair_all_residual_underlength,
    _residual_deficit,
    install_append_retry_guard,
)


class AppendRetryGuardTests(unittest.TestCase):
    def test_exact_two_additions_still_pass(self) -> None:
        result = _parse_safe_partial_additions(
            {
                "additions": [
                    {"id": "s2", "append_text": "إضافة أولى"},
                    {"id": "s3", "append_text": "إضافة ثانية"},
                ]
            },
            ["s2", "s3"],
        )
        self.assertEqual(result, {"s2": "إضافة أولى", "s3": "إضافة ثانية"})

    def test_run40_shape_single_valid_addition_is_accepted_without_synthesis(self) -> None:
        result = _parse_safe_partial_additions(
            {"additions": [{"id": "s2", "append_text": " ".join(["إضافة"] * 166)}]},
            ["s2", "s3"],
        )
        self.assertEqual(list(result), ["s2"])
        self.assertEqual(len(result["s2"].split()), 166)
        self.assertNotIn("s3", result)

    def test_single_addition_object_is_normalized_safely(self) -> None:
        result = _parse_safe_partial_additions(
            {"additions": {"id": "s3", "append_text": "إضافة آمنة"}},
            ["s2", "s3"],
        )
        self.assertEqual(result, {"s3": "إضافة آمنة"})

    def test_empty_or_missing_additions_still_fail_closed(self) -> None:
        for payload in ({}, {"additions": []}):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(RuntimeError, "between 1 and 2"):
                    _parse_safe_partial_additions(payload, ["s2", "s3"])

    def test_unknown_target_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "non-target section: s9"):
            _parse_safe_partial_additions(
                {"additions": [{"id": "s9", "append_text": "لا تقبل"}]},
                ["s2", "s3"],
            )

    def test_duplicate_target_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "duplicated section id: s2"):
            _parse_safe_partial_additions(
                {
                    "additions": [
                        {"id": "s2", "append_text": "أ"},
                        {"id": "s2", "append_text": "ب"},
                    ]
                },
                ["s2", "s3"],
            )

    def test_reversed_target_order_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "preserve target order"):
            _parse_safe_partial_additions(
                {
                    "additions": [
                        {"id": "s3", "append_text": "ثانية"},
                        {"id": "s2", "append_text": "أولى"},
                    ]
                },
                ["s2", "s3"],
            )

    def test_replacement_narration_field_is_ignored(self) -> None:
        result = _parse_safe_partial_additions(
            {
                "additions": [
                    {
                        "id": "s2",
                        "append_text": "الإضافة الوحيدة المقبولة",
                        "narration": "محاولة استبدال يجب تجاهلها",
                    }
                ]
            },
            ["s2", "s3"],
        )
        self.assertEqual(result, {"s2": "الإضافة الوحيدة المقبولة"})
        self.assertNotIn("محاولة استبدال", result["s2"])


class Attempt4ResidualSectionBandRegressionTests(unittest.TestCase):
    ATTEMPT4_COUNTS = [124, 97, 93, 104, 89, 85, 83, 126]

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
    def _plan(counts: list[int], *, terminal_closer: bool = False) -> staged.ProductionPlan:
        sections = Attempt4ResidualSectionBandRegressionTests._sections(counts)
        if terminal_closer and sections:
            closing_words = counts[-1]
            sections[-1].narration = " ".join(["ختام"] * max(0, closing_words - 1) + ["closer"])
        return staged.ProductionPlan(
            topic="صوت الآخرين في رأسك",
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
    def _restore_context() -> None:
        _RETRY_ATTEMPTED.set(False)
        _RETRY_ATTEMPTS.set(0)
        _FIRST_RETRY_BEFORE_DEFICIT.set(None)
        _SECOND_CHANCE_USED.set(False)

    def test_attempt4_shape_repairs_all_six_short_sections_even_when_total_already_passes_800(self) -> None:
        sections = self._sections(self.ATTEMPT4_COUNTS)
        self.assertEqual(sum(staged._word_count(s.narration) for s in sections), 801)
        prompts: list[str] = []

        def fake_json(api_key, prompt, model):
            del api_key, model
            prompts.append(prompt)
            return {
                "additions": [
                    {"id": f"sec_{index}", "append_text": " ".join(["إضافة"] * 35)}
                    for index in range(2, 8)
                ]
            }

        self._restore_context()
        try:
            with patch.object(staged, "json_text", side_effect=fake_json):
                additions = _repair_all_residual_underlength(
                    "key",
                    topic="صوت الآخرين في رأسك",
                    model="model",
                    sections=sections,
                    policy_json="{}",
                    research_json="{}",
                    narrative_format="problem_reveal_solution",
                    current_words=801,
                    minimum=800,
                )
        finally:
            self._restore_context()

        self.assertEqual(list(additions), [f"sec_{index}" for index in range(2, 8)])
        self.assertEqual(len(prompts), 1)
        for index in range(2, 8):
            self.assertIn(f'"id": "sec_{index}"', prompts[0])
        staged._append_retry_additions(sections, additions)
        for section in sections:
            words = staged._word_count(section.narration)
            self.assertGreaterEqual(words, 110)
            self.assertLessEqual(words, 170)
        self.assertLessEqual(sum(staged._word_count(s.narration) for s in sections), 1450)

    def test_post_build_guard_spends_one_call_for_attempt4_aggregate_pass_residuals(self) -> None:
        original_build = staged.build_plan
        original_retry = staged._script_doctor_underlength_retry
        original_parser = staged._parse_append_only_response
        original_json = staged.json_text
        calls: list[str] = []

        def fake_build(*args, **kwargs):
            del args, kwargs
            return self._plan(self.ATTEMPT4_COUNTS)

        def fake_json(api_key, prompt, model):
            del api_key, model
            calls.append(prompt)
            return {
                "additions": [
                    {"id": f"sec_{index}", "append_text": " ".join(["إضافة"] * 35)}
                    for index in range(2, 8)
                ]
            }

        try:
            staged.build_plan = fake_build
            staged.json_text = fake_json
            install_append_retry_guard()
            plan = staged.build_plan(
                "key",
                "صوت الآخرين في رأسك",
                "film",
                "model",
                research_context={},
            )
            self.assertEqual(len(calls), 1)
            for section in plan.sections:
                self.assertGreaterEqual(staged._word_count(section.narration), 110)
                self.assertLessEqual(staged._word_count(section.narration), 170)
        finally:
            staged.build_plan = original_build
            staged._script_doctor_underlength_retry = original_retry
            staged._parse_append_only_response = original_parser
            staged.json_text = original_json
            self._restore_context()


class Attempt5PartialAppendRegressionTests(unittest.TestCase):
    ATTEMPT5_AFTER_FIRST = [121, 119, 109, 106, 108, 99, 110, 81]

    @staticmethod
    def _plan(counts: list[int]) -> staged.ProductionPlan:
        return Attempt4ResidualSectionBandRegressionTests._plan(counts, terminal_closer=True)

    @staticmethod
    def _restore_context() -> None:
        Attempt4ResidualSectionBandRegressionTests._restore_context()

    def _run_guarded_fake_build(self, counts, *, first_before_deficit, fake_json):
        original_build = staged.build_plan
        original_retry = staged._script_doctor_underlength_retry
        original_parser = staged._parse_append_only_response
        original_json = staged.json_text

        def fake_build(*args, **kwargs):
            del args, kwargs
            _RETRY_ATTEMPTED.set(True)
            _RETRY_ATTEMPTS.set(1)
            _FIRST_RETRY_BEFORE_DEFICIT.set(first_before_deficit)
            return self._plan(counts)

        try:
            staged.build_plan = fake_build
            staged.json_text = fake_json
            install_append_retry_guard()
            return staged.build_plan("key", "topic", "film", "model", research_context={})
        finally:
            staged.build_plan = original_build
            staged._script_doctor_underlength_retry = original_retry
            staged._parse_append_only_response = original_parser
            staged.json_text = original_json
            self._restore_context()

    def test_attempt5_partial_first_append_unlocks_exactly_one_residual_only_followup(self) -> None:
        current_deficit = sum(max(0, 110 - value) for value in self.ATTEMPT5_AFTER_FIRST)
        self.assertEqual(current_deficit, 47)
        prompts: list[str] = []

        def fake_json(api_key, prompt, model):
            del api_key, model
            prompts.append(prompt)
            return {
                "additions": [
                    {"id": "sec_3", "append_text": " ".join(["إضافة"] * 7)},
                    {"id": "sec_4", "append_text": " ".join(["إضافة"] * 10)},
                    {"id": "sec_5", "append_text": " ".join(["إضافة"] * 8)},
                    {"id": "sec_6", "append_text": " ".join(["إضافة"] * 17)},
                    {"id": "sec_8", "append_text": " ".join(["إضافة"] * 35)},
                ]
            }

        plan = self._run_guarded_fake_build(
            self.ATTEMPT5_AFTER_FIRST,
            first_before_deficit=90,
            fake_json=fake_json,
        )
        self.assertEqual(len(prompts), 1)
        self.assertIn("SECOND AND FINAL partial-progress repair", prompts[0])
        self.assertNotIn('"id": "sec_1"', prompts[0])
        self.assertNotIn('"id": "sec_2"', prompts[0])
        self.assertNotIn('"id": "sec_7"', prompts[0])
        self.assertEqual(_residual_deficit(plan.sections), 0)
        self.assertTrue(plan.sections[-1].narration.endswith("closer"))
        for section in plan.sections:
            self.assertGreaterEqual(staged._word_count(section.narration), 110)
            self.assertLessEqual(staged._word_count(section.narration), 170)
        self.assertLessEqual(staged._plan_word_count(plan), 1450)

    def test_zero_progress_first_append_never_unlocks_second_call(self) -> None:
        current_deficit = sum(max(0, 110 - value) for value in self.ATTEMPT5_AFTER_FIRST)
        calls: list[str] = []

        def fake_json(api_key, prompt, model):
            del api_key, prompt, model
            calls.append("unexpected")
            return {"additions": [{"id": "sec_3", "append_text": "إضافة"}]}

        plan = self._run_guarded_fake_build(
            self.ATTEMPT5_AFTER_FIRST,
            first_before_deficit=current_deficit,
            fake_json=fake_json,
        )
        self.assertEqual(calls, [])
        self.assertEqual(_residual_deficit(plan.sections), current_deficit)

    def test_completed_first_append_never_unlocks_second_call(self) -> None:
        calls: list[str] = []

        def fake_json(api_key, prompt, model):
            del api_key, prompt, model
            calls.append("unexpected")
            return {"additions": []}

        plan = self._run_guarded_fake_build(
            [121, 119, 110, 110, 110, 110, 110, 110],
            first_before_deficit=90,
            fake_json=fake_json,
        )
        self.assertEqual(calls, [])
        self.assertEqual(_residual_deficit(plan.sections), 0)

    def test_partial_second_response_gets_no_third_call_and_remains_fail_closed(self) -> None:
        prompts: list[str] = []

        def fake_json(api_key, prompt, model):
            del api_key, model
            prompts.append(prompt)
            return {
                "additions": [
                    {"id": "sec_3", "append_text": " ".join(["إضافة"] * 7)},
                ]
            }

        plan = self._run_guarded_fake_build(
            self.ATTEMPT5_AFTER_FIRST,
            first_before_deficit=90,
            fake_json=fake_json,
        )
        self.assertEqual(len(prompts), 1)
        self.assertGreater(_residual_deficit(plan.sections), 0)
        self.assertTrue(plan.sections[-1].narration.endswith("closer"))

    def test_109_is_short_and_110_is_not(self) -> None:
        sections = Attempt4ResidualSectionBandRegressionTests._sections([109, 110])
        self.assertEqual(_residual_deficit(sections), 1)


if __name__ == "__main__":
    unittest.main()

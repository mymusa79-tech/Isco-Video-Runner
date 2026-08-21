from __future__ import annotations

import unittest
from unittest.mock import patch

import isco_video_agent.resilient_planner as staged

from scripts.append_retry_guard import (
    _RETRY_ATTEMPTED,
    _parse_safe_partial_additions,
    _repair_all_residual_underlength,
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
    def _plan(counts: list[int]) -> staged.ProductionPlan:
        sections = Attempt4ResidualSectionBandRegressionTests._sections(counts)
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
            _RETRY_ATTEMPTED.set(False)

    def test_post_build_guard_never_spends_second_append_call_if_engine_retry_was_already_used(self) -> None:
        original_build = staged.build_plan
        original_retry = staged._script_doctor_underlength_retry
        original_parser = staged._parse_append_only_response
        original_json = staged.json_text
        calls: list[str] = []

        def fake_build(*args, **kwargs):
            del args, kwargs
            _RETRY_ATTEMPTED.set(True)
            return self._plan(self.ATTEMPT4_COUNTS)

        def fake_json(api_key, prompt, model):
            del api_key, prompt, model
            calls.append("unexpected")
            return {"additions": []}

        try:
            staged.build_plan = fake_build
            staged.json_text = fake_json
            install_append_retry_guard()
            plan = staged.build_plan("key", "topic", "film", "model", research_context={})
            self.assertEqual(calls, [])
            self.assertTrue(any(staged._word_count(section.narration) < 110 for section in plan.sections))
        finally:
            staged.build_plan = original_build
            staged._script_doctor_underlength_retry = original_retry
            staged._parse_append_only_response = original_parser
            staged.json_text = original_json
            _RETRY_ATTEMPTED.set(False)


if __name__ == "__main__":
    unittest.main()

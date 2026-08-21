from __future__ import annotations

import unittest
from unittest.mock import patch

import isco_video_agent.resilient_planner as staged

from scripts.append_retry_guard import (
    _ACTIVE_CLOSER,
    _RETRY_ATTEMPTED,
    _parse_safe_partial_additions,
    _repair_all_residual_underlength,
    _residual_deficit,
    _validate_addition_bounds,
    install_append_retry_guard,
)


class AppendRetryGuardParserTests(unittest.TestCase):
    def test_complete_exact_additions_pass(self) -> None:
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

    def test_partial_subset_now_fails_closed(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "exactly 2 additions"):
            _parse_safe_partial_additions(
                {"additions": [{"id": "s2", "append_text": "إضافة"}]},
                ["s2", "s3"],
            )

    def test_missing_or_non_object_response_fails_closed(self) -> None:
        for payload in ({}, [], None):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(RuntimeError, "exactly 2 additions"):
                    _parse_safe_partial_additions(payload, ["s2", "s3"])

    def test_reversed_target_order_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "exactly these section ids in order"):
            _parse_safe_partial_additions(
                {
                    "additions": [
                        {"id": "s3", "append_text": "ثانية"},
                        {"id": "s2", "append_text": "أولى"},
                    ]
                },
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
            ["s2"],
        )
        self.assertEqual(result, {"s2": "الإضافة الوحيدة المقبولة"})

    def test_word_bounds_are_enforced_against_final_section_contract(self) -> None:
        specs = [
            {
                "id": "s2",
                "current_words": 106,
                "hard_section_band": [110, 170],
                "minimum_append_words": 4,
                "maximum_append_words": 6,
            },
        ]
        _validate_addition_bounds(
            {"s2": "واحد اثنان ثلاثة أربعة"}, specs, aggregate_headroom=20
        )
        with self.assertRaisesRegex(RuntimeError, "required final section band is 110-170"):
            _validate_addition_bounds(
                {"s2": "واحد اثنان ثلاثة"}, specs, aggregate_headroom=20
            )
        with self.assertRaisesRegex(RuntimeError, "maximum allowed is 6"):
            _validate_addition_bounds(
                {"s2": "واحد اثنان ثلاثة أربعة خمسة ستة سبعة"},
                specs,
                aggregate_headroom=20,
            )

    def test_attempt6_one_word_safety_undershoot_is_allowed_only_if_hard_floor_passes(self) -> None:
        spec = [
            {
                "id": "2",
                "current_words": 100,
                "hard_section_band": [110, 170],
                "minimum_append_words": 16,
                "maximum_append_words": 34,
            }
        ]
        _validate_addition_bounds(
            {"2": " ".join(["إضافة"] * 15)},
            spec,
            aggregate_headroom=100,
        )
        with self.assertRaisesRegex(RuntimeError, "would produce 109 section words"):
            _validate_addition_bounds(
                {"2": " ".join(["إضافة"] * 9)},
                spec,
                aggregate_headroom=100,
            )


class FilmResidualSectionBandRegressionTests(unittest.TestCase):
    ATTEMPT4_COUNTS = [124, 97, 93, 104, 89, 85, 83, 126]
    ATTEMPT5_COUNTS = [121, 119, 109, 106, 108, 99, 110, 81]
    ATTEMPT6_TRACE_COUNTS = [120, 100, 120, 120, 120, 120, 120, 120]

    @staticmethod
    def _sections(counts: list[int], *, terminal_closer: bool = False) -> list[staged.ScriptSection]:
        sections = [
            staged.ScriptSection(
                id=f"sec_{index}",
                narration=" ".join([f"كلمة{index}"] * words),
                visual_query="room notebook",
                key_point=f"distinct key point {index}",
            )
            for index, words in enumerate(counts, start=1)
        ]
        if terminal_closer and sections:
            closing_words = counts[-1]
            sections[-1].narration = " ".join(
                ["ختام"] * max(0, closing_words - 1) + ["closer"]
            )
        return sections

    @classmethod
    def _plan(cls, counts: list[int], *, terminal_closer: bool = False) -> staged.ProductionPlan:
        return staged.ProductionPlan(
            topic="صوت الآخرين في رأسك",
            pillar="understand",
            format="film",
            hook="hook",
            title_options=["a", "b", "c"],
            thumbnail_concepts=["a", "b", "c"],
            sections=cls._sections(counts, terminal_closer=terminal_closer),
            cta="cta",
            closing_payoff="payoff",
            identity_opener="opener",
            identity_closer="closer",
            identity_transitions=["t1", "t2", "t3"],
            narrative_format="problem_reveal_solution",
        )

    @staticmethod
    def _complete_additions(counts: list[int]) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        for index, words in enumerate(counts, start=1):
            if words < 110:
                append_words = 110 - words + 6
                result.append(
                    {
                        "id": f"sec_{index}",
                        "append_text": " ".join(["إضافة"] * append_words),
                    }
                )
        return result

    @staticmethod
    def _save_staged():
        return {
            "build_plan": staged.build_plan,
            "retry": staged._script_doctor_underlength_retry,
            "parser": staged._parse_append_only_response,
            "json": staged.json_text,
            "append": staged._append_retry_additions,
            "brand": staged._apply_brand_signature,
        }

    @staticmethod
    def _restore_staged(saved) -> None:
        staged.build_plan = saved["build_plan"]
        staged._script_doctor_underlength_retry = saved["retry"]
        staged._parse_append_only_response = saved["parser"]
        staged.json_text = saved["json"]
        staged._append_retry_additions = saved["append"]
        staged._apply_brand_signature = saved["brand"]
        _RETRY_ATTEMPTED.set(False)
        _ACTIVE_CLOSER.set(None)

    def test_attempt4_shape_repairs_all_six_short_sections_in_one_call(self) -> None:
        sections = self._sections(self.ATTEMPT4_COUNTS)
        self.assertEqual(sum(staged._word_count(s.narration) for s in sections), 801)
        calls: list[str] = []

        def fake_json(api_key, prompt, model):
            del api_key, model
            calls.append(prompt)
            return {"additions": self._complete_additions(self.ATTEMPT4_COUNTS)}

        _RETRY_ATTEMPTED.set(False)
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
        self.assertEqual(len(calls), 1)
        self.assertEqual(list(additions), [f"sec_{index}" for index in range(2, 8)])
        staged._append_retry_additions(sections, additions)
        for section in sections:
            self.assertGreaterEqual(staged._word_count(section.narration), 110)
            self.assertLessEqual(staged._word_count(section.narration), 170)
        self.assertLessEqual(sum(staged._word_count(s.narration) for s in sections), 1450)
        _RETRY_ATTEMPTED.set(False)

    def test_attempt5_post_build_repairs_residuals_and_closing_before_anchor_in_one_call(self) -> None:
        saved = self._save_staged()
        calls: list[str] = []

        def fake_build(*args, **kwargs):
            del args, kwargs
            return self._plan(self.ATTEMPT5_COUNTS, terminal_closer=True)

        def fake_json(api_key, prompt, model):
            del api_key, model
            calls.append(prompt)
            return {"additions": self._complete_additions(self.ATTEMPT5_COUNTS)}

        try:
            staged.build_plan = fake_build
            staged.json_text = fake_json
            install_append_retry_guard()
            plan = staged.build_plan("key", "topic", "film", "model", research_context={})
            self.assertEqual(len(calls), 1)
            self.assertIn('"id": "sec_8"', calls[0])
            self.assertEqual(_residual_deficit(plan.sections), 0)
            self.assertTrue(plan.sections[-1].narration.endswith("closer"))
            self.assertEqual(plan.sections[-1].narration.count("closer"), 1)
            for section in plan.sections:
                self.assertGreaterEqual(staged._word_count(section.narration), 110)
                self.assertLessEqual(staged._word_count(section.narration), 170)
            self.assertLessEqual(staged._plan_word_count(plan), 1450)
        finally:
            self._restore_staged(saved)

    def test_attempt6_trace_accepts_15_when_preferred_is_16_but_final_is_115(self) -> None:
        sections = self._sections(self.ATTEMPT6_TRACE_COUNTS)
        calls = 0

        def fake_json(api_key, prompt, model):
            nonlocal calls
            del api_key, model
            calls += 1
            self.assertIn('"minimum_append_words": 16', prompt)
            return {
                "additions": [
                    {
                        "id": "sec_2",
                        "append_text": " ".join(["إضافة"] * 15),
                    }
                ]
            }

        _RETRY_ATTEMPTED.set(False)
        with patch.object(staged, "json_text", side_effect=fake_json):
            additions = _repair_all_residual_underlength(
                "key",
                topic="صوت الآخرين في رأسك",
                model="model",
                sections=sections,
                policy_json="{}",
                research_json="{}",
                narrative_format="problem_reveal_solution",
                current_words=sum(self.ATTEMPT6_TRACE_COUNTS),
                minimum=800,
            )
        self.assertEqual(calls, 1)
        staged._append_retry_additions(sections, additions)
        self.assertEqual(staged._word_count(sections[1].narration), 115)
        self.assertEqual(_residual_deficit(sections), 0)
        _RETRY_ATTEMPTED.set(False)

    def test_internal_engine_retry_can_repair_closing_before_anchor_in_same_single_call(self) -> None:
        saved = self._save_staged()
        calls: list[str] = []

        def fake_json(api_key, prompt, model):
            del api_key, model
            calls.append(prompt)
            return {"additions": self._complete_additions(self.ATTEMPT5_COUNTS)}

        def fake_build(*args, **kwargs):
            del args, kwargs
            plan = self._plan(self.ATTEMPT5_COUNTS, terminal_closer=True)
            staged._apply_brand_signature(plan.sections, "film", "", plan.identity_closer)
            additions = staged._script_doctor_underlength_retry(
                "key",
                topic=plan.topic,
                model="model",
                sections=plan.sections,
                policy_json="{}",
                research_json="{}",
                narrative_format=plan.narrative_format,
                current_words=staged._plan_word_count(plan),
                minimum=800,
            )
            staged._append_retry_additions(plan.sections, additions)
            return plan

        try:
            staged.build_plan = fake_build
            staged.json_text = fake_json
            install_append_retry_guard()
            plan = staged.build_plan("key", "topic", "film", "model", research_context={})
            self.assertEqual(len(calls), 1)
            self.assertEqual(_residual_deficit(plan.sections), 0)
            self.assertTrue(plan.sections[-1].narration.endswith("closer"))
            self.assertEqual(plan.sections[-1].narration.count("closer"), 1)
        finally:
            self._restore_staged(saved)

    def test_missing_target_fails_closed_after_one_provider_call(self) -> None:
        sections = self._sections(self.ATTEMPT5_COUNTS, terminal_closer=True)
        calls = 0

        def fake_json(api_key, prompt, model):
            nonlocal calls
            del api_key, prompt, model
            calls += 1
            complete = self._complete_additions(self.ATTEMPT5_COUNTS)
            return {"additions": complete[:-1]}

        _RETRY_ATTEMPTED.set(False)
        with patch.object(staged, "json_text", side_effect=fake_json):
            with self.assertRaisesRegex(RuntimeError, "exactly 5 additions"):
                _repair_all_residual_underlength(
                    "key",
                    topic="topic",
                    model="model",
                    sections=sections,
                    policy_json="{}",
                    research_json="{}",
                    narrative_format="problem_reveal_solution",
                    current_words=853,
                    minimum=800,
                )
        self.assertEqual(calls, 1)
        _RETRY_ATTEMPTED.set(False)

    def test_too_short_provider_addition_fails_closed_after_one_call(self) -> None:
        sections = self._sections(self.ATTEMPT5_COUNTS, terminal_closer=True)
        calls = 0

        def fake_json(api_key, prompt, model):
            nonlocal calls
            del api_key, prompt, model
            calls += 1
            additions = self._complete_additions(self.ATTEMPT5_COUNTS)
            additions[0]["append_text"] = "قصير"
            return {"additions": additions}

        _RETRY_ATTEMPTED.set(False)
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
                    current_words=853,
                    minimum=800,
                )
        self.assertEqual(calls, 1)
        _RETRY_ATTEMPTED.set(False)

    def test_post_build_guard_never_spends_second_call_after_internal_retry(self) -> None:
        saved = self._save_staged()
        calls: list[str] = []

        def fake_build(*args, **kwargs):
            del args, kwargs
            _RETRY_ATTEMPTED.set(True)
            return self._plan(self.ATTEMPT5_COUNTS, terminal_closer=True)

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
            self.assertGreater(_residual_deficit(plan.sections), 0)
            self.assertTrue(plan.sections[-1].narration.endswith("closer"))
        finally:
            self._restore_staged(saved)

    def test_109_is_short_and_110_is_not(self) -> None:
        sections = self._sections([109, 110])
        self.assertEqual(_residual_deficit(sections), 1)


if __name__ == "__main__":
    unittest.main()

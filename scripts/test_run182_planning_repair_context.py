from __future__ import annotations

import copy
import json
import unittest
from unittest.mock import patch

from clean_v2 import pipeline
from clean_v2.providers import (
    MAX_PROMPT_BYTES,
    ProviderAdapter,
    ProviderRouter,
    _mistral_planning_validator_retry_prompt,
    _safe_mistral_planning_raw_diagnostic,
)
from clean_v2.short_format import ShortFormatError
from scripts.test_clean_v2_unified_visual_story import _brief, _planning_value


def duplicate_plan() -> dict:
    value = _planning_value("short")
    value["practical_action_ar"] = "اكتب أول جملة في الدفتر المغلق."
    beat = value["visual_story"]["beats"][2]
    beat["stock_query_en"] = value["visual_story"]["beats"][0]["stock_query_en"]
    beat["shot_intent"] = beat["stock_query_en"]
    return value


def previous_draft(prompt: str) -> dict:
    serialized = prompt.split("\n\nPREVIOUS_PLANNING_JSON:\n", 1)[1]
    return json.loads(serialized.split("\n\nMISTRAL_PLANNING_VALIDATOR_RETRY", 1)[0])


class Run182PlanningRepairContextTests(unittest.TestCase):
    def test_real_duplicate_rejection_identifies_both_beats(self) -> None:
        with self.assertRaises(ValueError) as caught:
            pipeline._validate_plan_for_brief(duplicate_plan(), _brief("short"))
        self.assertEqual(str(caught.exception), "visual_story stock_query_en values must be distinct per beat")
        self.assertEqual(caught.exception.planning_repair_context, {
            "beat_id": "b3", "conflicting_beat_id": "b1",
        })

    def test_existing_two_retries_repair_actual_drafts_and_retain_prior_fix(self) -> None:
        initial = duplicate_plan()
        original = copy.deepcopy(initial)
        calls = []
        returned = []

        def invoke(prompt: str, _max_tokens: int, stage: str) -> dict:
            self.assertEqual(stage, "planning")
            calls.append(prompt)
            if len(calls) == 1:
                candidate = copy.deepcopy(initial)
            else:
                candidate = previous_draft(prompt)
                self.assertEqual(candidate, returned[-1])
                if len(calls) == 2:
                    self.assertIn("Beat b3 conflicts with beat b1", prompt)
                    candidate["visual_story"]["beats"][2] = copy.deepcopy(
                        _planning_value("short")["visual_story"]["beats"][2]
                    )
                    candidate["practical_action_ar"] = "اختر مهمة الآن."
                else:
                    self.assertIn("short_practical_action_too_generic_for_topic", prompt)
                    self.assertIn("Earlier rules corrected", prompt)
                    self.assertIn("stock_query_en values must be distinct per beat", prompt)
                    candidate["practical_action_ar"] = initial["practical_action_ar"]
            returned.append(copy.deepcopy(candidate))
            return candidate

        router = ProviderRouter((ProviderAdapter(
            "mistral", invoke, stages=frozenset({"planning"}), accepts_stage=True,
        ),))
        result = router.route(
            stage="planning", prompt=pipeline._planning_prompt(_brief("short")),
            max_tokens=3000,
            validator=lambda value: pipeline._validate_plan_for_brief(value, _brief("short")),
        )
        self.assertEqual(len(calls), 3)
        self.assertEqual(initial, original)
        self.assertEqual(result["practical_action_ar"], initial["practical_action_ar"])
        self.assertEqual(result["sections"], pipeline._validate_plan_for_brief(returned[-1], _brief("short"))["sections"])
        queries = [beat["stock_query_en"] for beat in result["visual_story"]["beats"]]
        self.assertEqual(len(queries), len(set(queries)))
        self.assertEqual(len(queries), 7)
        self.assertEqual([event["result"] for event in router.events], ["retrying", "retrying", "success"])
        first_detail = json.loads(router.events[0]["detail"])
        self.assertEqual(first_detail["repair_context"]["beat_id"], "b3")
        self.assertNotIn(initial["title"], router.events[0]["detail"])
        self.assertTrue(all(len(prompt.encode("utf-8")) <= MAX_PROMPT_BYTES for prompt in calls))

    def test_uncorrected_output_keeps_gates_budget_and_existing_provider_fallback(self) -> None:
        calls = []

        def mistral(prompt, _tokens, _stage):
            calls.append("mistral")
            return duplicate_plan()

        def gemini(prompt, _tokens, _stage):
            calls.append("gemini_flash_lite")
            value = _planning_value("short")
            value["practical_action_ar"] = "اكتب أول جملة في الدفتر المغلق."
            return value

        router = ProviderRouter(tuple(
            ProviderAdapter(name, call, stages=frozenset({"planning"}), accepts_stage=True)
            for name, call in (("mistral", mistral), ("gemini_flash_lite", gemini))
        ))
        with patch("builtins.print"):
            result = router.route(
                stage="planning", prompt="BASE", max_tokens=3000,
                validator=lambda value: pipeline._validate_plan_for_brief(value, _brief("short")),
            )
        self.assertEqual(calls, ["mistral", "mistral", "mistral", "gemini_flash_lite"])
        self.assertEqual(len(result["visual_story"]["beats"]), 7)
        rejected = next(event for event in router.events if event["result"] == "invalid_output")
        self.assertEqual(json.loads(rejected["detail"])["repair_context"]["conflicting_beat_id"], "b1")

    def test_generic_action_still_fails_production_validator(self) -> None:
        value = _planning_value("short")
        value["practical_action_ar"] = "اختر مهمة الآن."
        with self.assertRaisesRegex(ShortFormatError, "short_practical_action_too_generic_for_topic"):
            pipeline._validate_plan_for_brief(value, _brief("short"))
        prompt = _mistral_planning_validator_retry_prompt(
            "BASE", ShortFormatError("short_practical_action_too_generic_for_topic"), candidate=value,
        )
        self.assertEqual(previous_draft(prompt), value)
        self.assertIn("existing s1 blockage", prompt)
        self.assertIn("existing s3 result", prompt)

    def test_snapshot_preserves_each_product_plan_without_reformatting(self) -> None:
        for fmt in ("short", "film", "podcast"):
            with self.subTest(fmt=fmt):
                value = _planning_value(fmt)
                prompt = _mistral_planning_validator_retry_prompt(
                    pipeline._planning_prompt(_brief(fmt)), ValueError("rejected rule"), candidate=value,
                )
                self.assertEqual(previous_draft(prompt), value)
                self.assertLessEqual(len(prompt.encode("utf-8")), MAX_PROMPT_BYTES)

    def test_oversized_draft_is_omitted_whole_within_provider_prompt_budget(self) -> None:
        error = ValueError("rejected rule")
        plain = _mistral_planning_validator_retry_prompt("BASE", error)
        limit = len(plain.encode("utf-8")) + 30
        prompt = _mistral_planning_validator_retry_prompt(
            "BASE", error, candidate={"title": "نص طويل" * 1000}, max_prompt_bytes=limit,
        )
        self.assertEqual(prompt, plain)
        self.assertNotIn("PREVIOUS_PLANNING_JSON:\n", prompt)
        self.assertLessEqual(len(prompt.encode("utf-8")), limit)
        self.assertIsNone(_mistral_planning_validator_retry_prompt("BASE", error, max_prompt_bytes=4))

    def test_invalid_draft_shape_does_not_break_retry_routing(self) -> None:
        error = ValueError("rejected rule")
        for candidate in (None, ["invalid"], {"unserializable": object()}, {"nan": float("nan")}):
            with self.subTest(candidate=type(candidate).__name__):
                prompt = _mistral_planning_validator_retry_prompt("BASE", error, candidate=candidate)
                self.assertIsNotNone(prompt)
                self.assertNotIn("PREVIOUS_PLANNING_JSON:\n", prompt)

    def test_rejection_locations_are_sanitized_and_authored_text_stays_out_of_diagnostics(self) -> None:
        error = ValueError("visual_story stock_query_en values must be distinct per beat")
        error.planning_repair_context = {"beat_id": "b3", "conflicting_beat_id": "SECRET CONTENT", "other": "SECRET"}
        diagnostic = _safe_mistral_planning_raw_diagnostic(json.dumps({"title": "SECRET_TITLE"}), error)
        self.assertEqual(diagnostic["repair_context"], {"beat_id": "b3"})
        self.assertNotIn("SECRET", json.dumps(diagnostic))


if __name__ == "__main__":
    unittest.main()

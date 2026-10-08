from __future__ import annotations

import copy
import json
import unittest
from unittest.mock import patch

from clean_v2 import pipeline
from clean_v2.contracts import LONGFORM_NARRATIVE_FORMATS
from clean_v2.providers import (
    MAX_PROMPT_BYTES,
    ProviderAdapter,
    ProviderRouter,
    _mistral_planning_validator_retry_prompt,
    _safe_mistral_planning_raw_diagnostic,
)
from clean_v2.short_format import TEMPLATE_ORDER
from clean_v2.visual_story import CHANNEL_VISUAL_IDENTITY, MAX_PLANNING_REPAIR_BEATS
from scripts.test_clean_v2_unified_visual_story import _brief, _planning_value
from scripts.test_run182_planning_repair_context import previous_draft


def healthy_plan(fmt="short"):
    value = _planning_value(fmt)
    value["visual_story"]["visual_world"] = CHANNEL_VISUAL_IDENTITY
    if fmt == "short":
        value["practical_action_ar"] = "اكتب أول جملة في الدفتر المغلق."
    return value


def scene(value, index, query):
    beat = value["visual_story"]["beats"][index]
    beat.update(shot_intent=query, stock_query_en=query, semantic_must_have=[query])
    beat.pop("stock_query_alt_en", None)


def family_conflict():
    value = healthy_plan()
    scene(value, 2, "hands sorting paper task cards into one chosen stack")
    scene(value, 3, "hands crossing out two paper options leaving one card")
    return value


def combined_conflict():
    value = family_conflict()
    scene(value, 5, "person sitting at desk using laptop")
    value["sections"][2]["visual_query_alt_en"] = "person scrolling phone while sitting at desk"
    return value


def rejected(value, fmt="short"):
    try:
        pipeline._validate_plan_for_brief(value, _brief(fmt), enforce_visual_identity=True)
    except ValueError as exc:
        return exc
    raise AssertionError("fixture must fail the actual production validator")


class PlanningVisualRepairFeedbackTests(unittest.TestCase):
    def test_first_semantic_rejection_also_identifies_hidden_family_conflicts(self):
        value = combined_conflict()
        original = copy.deepcopy(value)
        error = rejected(value)
        self.assertIn("beat b6 post-hook semantic drop", str(error))
        context = error.planning_repair_context
        self.assertEqual(context["post_hook_weak_beat_ids"], ["b6"])
        self.assertEqual(context["family_beat_ids"]["stationery"], ["b1", "b3", "b4"])
        self.assertEqual(context["overused_families"], ["stationery"])
        self.assertIn(["b3", "b4"], context["same_family_neighbors"])
        self.assertEqual(context["visual_family_limit"], 2)
        self.assertEqual(value, original)

    def test_family_gate_keeps_its_rejection_and_reports_all_affected_beats(self):
        error = rejected(family_conflict())
        self.assertEqual(str(error), "visual_story Short visual family exceeds two beats: stationery")
        prompt = _mistral_planning_validator_retry_prompt("BASE", error, candidate=family_conflict())
        self.assertIn("at most TWO beats", prompt)
        self.assertIn("not per section", prompt)
        self.assertIn('"stationery":["b1","b3","b4"]', prompt)
        self.assertIn("Fix ALL listed visual conflicts together", prompt)
        self.assertIn("Writing, notebooks, paper cards and checklists are all stationery", prompt)

    def test_last_beat_process_rejection_has_a_targeted_result_correction(self):
        value = healthy_plan()
        scene(value, 6, "hands selecting one priority card then writing next priority in notebook")
        error = rejected(value)
        self.assertIn("Short payoff must show the visible result/state", str(error))
        self.assertEqual(error.planning_repair_context["payoff_process_beat_id"], "b7")
        prompt = _mistral_planning_validator_retry_prompt("BASE", error, candidate=value)
        self.assertIn("visible changed state AFTER the locked practical action", prompt)
        self.assertIn('"payoff_process_beat_id":"b7"', prompt)

    def test_family_feedback_follows_the_same_authored_alternate_as_the_gate(self):
        value = family_conflict()
        scene(value, 3, "person sitting at desk using laptop")
        value["visual_story"]["beats"][3]["stock_query_alt_en"] = "hands crossing out two paper options leaving one card"
        error = rejected(value)
        self.assertIn("family exceeds two beats: stationery", str(error))
        self.assertEqual(error.planning_repair_context["family_beat_ids"]["stationery"], ["b1", "b3", "b4"])
        self.assertNotIn("b4", error.planning_repair_context.get("post_hook_weak_beat_ids", []))

    def test_combined_defects_can_be_repaired_in_the_first_existing_correction(self):
        initial = combined_conflict()
        calls = []
        corrected = None

        def invoke(prompt, _tokens, _stage):
            nonlocal corrected
            calls.append(prompt)
            if len(calls) == 1:
                return copy.deepcopy(initial)
            corrected = previous_draft(prompt)
            self.assertEqual(corrected, initial)
            self.assertIn('"post_hook_weak_beat_ids":["b6"]', prompt)
            self.assertIn('"overused_families":["stationery"]', prompt)
            self.assertIn("Fix ALL listed visual conflicts together", prompt)
            good = healthy_plan()
            for index in (2, 3, 5):
                corrected["visual_story"]["beats"][index] = good["visual_story"]["beats"][index]
            return corrected

        router = ProviderRouter((ProviderAdapter("mistral", invoke, stages=frozenset({"planning"}), accepts_stage=True),))
        result = router.route(stage="planning", prompt=pipeline._planning_prompt(_brief("short")), max_tokens=3000,
                              validator=lambda v: pipeline._validate_plan_for_brief(v, _brief("short")))
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["practical_action_ar"], initial["practical_action_ar"])
        self.assertEqual(corrected["sections"], initial["sections"])
        self.assertEqual(corrected["visual_story"]["retention_thread"], initial["visual_story"]["retention_thread"])
        for index in (0, 1, 4, 6):
            self.assertEqual(corrected["visual_story"]["beats"][index], initial["visual_story"]["beats"][index])
        self.assertEqual([e["result"] for e in router.events], ["retrying", "success"])
        diagnostic = json.loads(router.events[0]["detail"])
        self.assertEqual(diagnostic["repair_context"]["overused_families"], ["stationery"])

    def test_second_correction_is_targeted_and_retains_the_first_semantic_fix(self):
        calls = []
        last_returned = None

        def invoke(prompt, _tokens, _stage):
            nonlocal last_returned
            calls.append(prompt)
            if len(calls) == 1:
                candidate = combined_conflict()
            else:
                candidate = previous_draft(prompt)
                self.assertEqual(candidate, last_returned)
                good = healthy_plan()
                if len(calls) == 2:
                    candidate["visual_story"]["beats"][5] = good["visual_story"]["beats"][5]
                else:
                    self.assertIn("at most TWO beats", prompt)
                    self.assertIn("Earlier rules corrected", prompt)
                    self.assertIn("post-hook semantic drop", prompt)
                    self.assertEqual(candidate["visual_story"]["beats"][5], good["visual_story"]["beats"][5])
                    for index in (2, 3):
                        candidate["visual_story"]["beats"][index] = good["visual_story"]["beats"][index]
            last_returned = copy.deepcopy(candidate)
            return candidate

        router = ProviderRouter((ProviderAdapter("mistral", invoke, stages=frozenset({"planning"}), accepts_stage=True),))
        result = router.route(stage="planning", prompt="BASE", max_tokens=3000,
                              validator=lambda v: pipeline._validate_plan_for_brief(v, _brief("short")))
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(result["visual_story"]["beats"]), 7)

    def test_ignored_correction_still_rejects_with_the_same_budget_and_fallback(self):
        calls = []
        def invoke_mistral(*args):
            calls.append("mistral")
            return combined_conflict()
        def invoke_gemini(*args):
            calls.append("gemini_flash_lite")
            return healthy_plan()
        router = ProviderRouter(tuple(ProviderAdapter(name, invoke, stages=frozenset({"planning"}), accepts_stage=True)
                                      for name, invoke in (("mistral", invoke_mistral), ("gemini_flash_lite", invoke_gemini))))
        with patch("builtins.print"):
            result = router.route(stage="planning", prompt="BASE", max_tokens=3000,
                                  validator=lambda v: pipeline._validate_plan_for_brief(v, _brief("short")))
        self.assertEqual(calls, ["mistral", "mistral", "mistral", "gemini_flash_lite"])
        self.assertEqual(len(result["visual_story"]["beats"]), 7)
        invalid = next(e for e in router.events if e["result"] == "invalid_output")
        self.assertEqual(json.loads(invalid["detail"])["repair_context"]["post_hook_weak_beat_ids"], ["b6"])

    def test_conflict_diagnostics_are_bounded_and_exclude_authored_content(self):
        error = ValueError("rejected rule")
        error.planning_repair_context = {
            "family_beat_ids": {"stationery": [f"b{i}" for i in range(200)], "SECRET_QUERY": ["b1"]},
            "overused_families": ["stationery", "SECRET_QUERY"],
            "post_hook_weak_beat_ids": ["b6", "private authored words", "نص"],
            "same_family_neighbors": [["b3", "b4"], ["b3", "private authored words"]],
            "visual_family_limit": 99,
            "query": "SECRET_QUERY",
        }
        diagnostic = _safe_mistral_planning_raw_diagnostic(json.dumps({"title": "SECRET_TITLE"}), error)
        context = diagnostic["repair_context"]
        self.assertEqual(len(context["family_beat_ids"]["stationery"]), MAX_PLANNING_REPAIR_BEATS)
        self.assertEqual(context["post_hook_weak_beat_ids"], ["b6"])
        self.assertEqual(context["same_family_neighbors"], [["b3", "b4"]])
        self.assertNotIn("visual_family_limit", context)
        self.assertNotIn("SECRET", json.dumps(diagnostic))
        self.assertNotIn("private authored words", json.dumps(diagnostic))

    def test_conflict_feedback_and_snapshot_obey_the_admitted_prompt_ceiling(self):
        error = rejected(combined_conflict())
        plain = _mistral_planning_validator_retry_prompt("BASE", error)
        limit = len(plain.encode("utf-8")) + 20
        prompt = _mistral_planning_validator_retry_prompt("BASE", error, candidate=combined_conflict(), max_prompt_bytes=limit)
        self.assertEqual(prompt, plain)
        self.assertIn("PLANNING_VISUAL_REPAIR_CONTEXT", prompt)
        self.assertNotIn("PREVIOUS_PLANNING_JSON:\n", prompt)
        self.assertLessEqual(len(prompt.encode("utf-8")), limit)
        self.assertLessEqual(len(plain.encode("utf-8")), MAX_PROMPT_BYTES)


class AllTemplatesVisualRepairTests(unittest.TestCase):
    def check_template(self, fmt, template):
        brief = _brief(fmt)
        profile = (pipeline.select_short_template(brief) if fmt == "short"
                   else pipeline._select_longform_narrative_profile(brief))
        key = "template" if fmt == "short" else "narrative_format"
        profile[key] = template
        selector = "select_short_template" if fmt == "short" else "_select_longform_narrative_profile"
        good = healthy_plan(fmt)
        bad = copy.deepcopy(good)
        index = 5 if fmt == "short" else 1
        section_index = 2 if fmt == "short" else 1
        scene(bad, index, "person sitting at desk using laptop")
        bad["sections"][section_index]["visual_query_alt_en"] = "person scrolling phone while sitting at desk"
        calls = []
        def invoke(prompt, _tokens, _stage):
            calls.append(prompt)
            if len(calls) == 1:
                return copy.deepcopy(bad)
            candidate = previous_draft(prompt)
            self.assertEqual(candidate, bad)
            self.assertIn("post_hook_weak_beat_ids", prompt)
            candidate["visual_story"]["beats"][index] = copy.deepcopy(good["visual_story"]["beats"][index])
            return candidate
        router = ProviderRouter((ProviderAdapter("mistral", invoke, stages=frozenset({"planning"}), accepts_stage=True),))
        with patch.object(pipeline, selector, return_value=profile):
            result = router.route(stage="planning", prompt=pipeline._planning_prompt(brief), max_tokens=3000,
                                  validator=lambda v: pipeline._validate_plan_for_brief(v, brief))
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["short_template" if fmt == "short" else "narrative_format"], template)
        self.assertEqual([s["id"] for s in result["sections"]], [s["id"] for s in good["sections"]])
        self.assertEqual(len(result["visual_story"]["beats"]), len(good["visual_story"]["beats"]))


def template_case(fmt, template):
    def test(self):
        self.check_template(fmt, template)
    return test


for _fmt, _templates in (("short", TEMPLATE_ORDER), ("film", sorted(LONGFORM_NARRATIVE_FORMATS)), ("podcast", ("dialogue_qa",))):
    for _template in _templates:
        setattr(AllTemplatesVisualRepairTests, f"test_{_fmt}_{_template}", template_case(_fmt, _template))


if __name__ == "__main__":
    unittest.main()

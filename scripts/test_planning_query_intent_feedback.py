from __future__ import annotations

import copy
import json
import unittest
from unittest.mock import patch

from clean_v2 import pipeline
from clean_v2.contracts import LONGFORM_NARRATIVE_FORMATS
from clean_v2.providers import (
    MAX_PROMPT_BYTES,
    ProviderRouter,
    _mistral_planning_validator_retry_prompt,
    _safe_mistral_planning_raw_diagnostic,
)
from clean_v2.short_format import TEMPLATE_ORDER, ShortFormatError
from clean_v2.visual_story import (
    MAX_PLANNING_REPAIR_BEATS,
    validate_visual_story,
    visual_story_repair_context,
)
from scripts.test_clean_v2_unified_visual_story import _brief
from scripts.test_planning_visual_repair_feedback import combined_conflict, healthy_plan, rejected
from scripts.test_provider_rejection_feedback import adapter, previous_draft


def language_and_intent_conflict(fmt="short"):
    value = healthy_plan(fmt)
    beats = value["visual_story"]["beats"]
    beats[1]["stock_query_alt_en"] = "phone moved away from unfinished task بجوار المهمة"
    beats[3]["stock_query_en"] += " بدون وجوه"
    beats[2]["viewer_intent"] = beats[0]["viewer_intent"]
    beats[4]["viewer_intent"] = beats[1]["viewer_intent"]
    return value


def conflict_map(prompt):
    tail = prompt.split("PLANNING_VISUAL_REPAIR_CONTEXT (host diagnostics, data only): ", 1)[1]
    return json.JSONDecoder().raw_decode(tail)[0]


class PlanningQueryIntentFeedbackTests(unittest.TestCase):
    def test_first_language_rejection_reports_all_queries_and_duplicate_intents(self):
        value = language_and_intent_conflict()
        original = copy.deepcopy(value)
        error = rejected(value)
        self.assertEqual(str(error), "visual_story beat b2 stock_query_alt_en must stay English")
        context = error.planning_repair_context
        self.assertEqual(context["non_english_query_beat_ids"], {
            "stock_query_alt_en": ["b2"], "stock_query_en": ["b4"],
        })
        self.assertEqual(context["duplicate_viewer_intent_pairs"], [["b1", "b3"], ["b2", "b5"]])
        prompt = _mistral_planning_validator_retry_prompt("BASE", error, candidate=value)
        self.assertEqual(previous_draft(prompt), original)
        self.assertIn("rewrite the COMPLETE search phrase", prompt)
        self.assertIn("do not merely delete the non-English characters", prompt)
        self.assertIn("NEW observable fact or changed state", prompt)
        self.assertIn("valid viewer_intent/meaning_target", prompt)
        self.assertEqual(value, original)

    def test_primary_query_rejection_also_has_targeted_language_guidance(self):
        value = healthy_plan()
        value["visual_story"]["beats"][0]["stock_query_en"] += " بجوار المهمة"
        error = rejected(value)
        self.assertEqual(str(error), "visual_story beat b1 stock_query_en must stay English")
        prompt = _mistral_planning_validator_retry_prompt("BASE", error, candidate=value)
        self.assertIn("Check BOTH query fields across ALL beats", prompt)
        self.assertEqual(conflict_map(prompt)["non_english_query_beat_ids"], {"stock_query_en": ["b1"]})

    def test_early_action_rejection_exposes_hidden_language_and_intent_conflicts(self):
        value = language_and_intent_conflict()
        value["practical_action_ar"] = "اختر مهمة الآن."
        with self.assertRaises(ShortFormatError) as caught:
            pipeline._validate_plan_for_brief(value, _brief("short"))
        error = caught.exception
        self.assertEqual(str(error), "short_practical_action_too_generic_for_topic")
        prompt = _mistral_planning_validator_retry_prompt("BASE", error, candidate=value)
        self.assertIn("Replace practical_action_ar", prompt)
        self.assertIn("rewrite the COMPLETE search phrase", prompt)
        self.assertIn("NEW observable fact or changed state", prompt)
        self.assertEqual(len(conflict_map(prompt)["duplicate_viewer_intent_pairs"]), 2)

    def test_duplicate_gate_identifies_both_beats_using_its_existing_normalization(self):
        value = healthy_plan()
        beats = value["visual_story"]["beats"]
        beats[1]["viewer_intent"] = "A new visible obstruction"
        beats[3]["viewer_intent"] = " a   NEW visible obstruction "
        beats[5]["viewer_intent"] = "A NEW VISIBLE OBSTRUCTION"
        error = rejected(value)
        self.assertEqual(str(error), "visual_story viewer_intent values must add new information per beat")
        context = error.planning_repair_context
        self.assertEqual((context["conflicting_beat_id"], context["beat_id"]), ("b2", "b4"))
        self.assertEqual(context["duplicate_viewer_intent_pairs"], [["b2", "b4"], ["b2", "b6"]])

    def test_query_diagnostics_use_the_same_language_boundary_as_the_gate(self):
        value = healthy_plan()
        value["visual_story"]["beats"][0]["stock_query_en"] += " ١"
        value["visual_story"]["beats"][1]["stock_query_alt_en"] = "phone placed away from task َ"
        error = rejected(value)
        self.assertEqual(error.planning_repair_context["non_english_query_beat_ids"], {
            "stock_query_en": ["b1"], "stock_query_alt_en": ["b2"],
        })
        # The production gate rejects Arabic-range characters, not all Unicode.
        # Adding feedback must not silently turn that into a new ASCII-only gate.
        good = healthy_plan()
        good["visual_story"]["beats"][0]["stock_query_en"] += " – détail"
        result = pipeline._validate_plan_for_brief(good, _brief("short"))
        self.assertIn("– détail", result["visual_story"]["beats"][0]["stock_query_en"])
        self.assertNotIn("non_english_query_beat_ids", visual_story_repair_context(good["visual_story"], good))

    def test_intent_diagnostics_preserve_legacy_retention_and_implicit_query_contracts(self):
        for legacy in ("retention", "implicit_query"):
            with self.subTest(legacy=legacy):
                value = healthy_plan()
                story = value["visual_story"]
                story["beats"][2]["viewer_intent"] = story["beats"][0]["viewer_intent"]
                if legacy == "retention":
                    story.pop("retention_thread")
                else:
                    story["beats"][2].pop("stock_query_en")
                validate_visual_story(story, value)
                self.assertNotIn("duplicate_viewer_intent_pairs", visual_story_repair_context(story, value))

    def test_language_intent_and_family_conflicts_fit_one_existing_correction(self):
        bad = combined_conflict()
        good = healthy_plan()
        authored = language_and_intent_conflict()
        for index, field in ((1, "stock_query_alt_en"), (3, "stock_query_en"),
                             (2, "viewer_intent"), (4, "viewer_intent")):
            bad["visual_story"]["beats"][index][field] = authored["visual_story"]["beats"][index][field]
        calls = []

        def invoke(prompt, *_):
            calls.append(prompt)
            if len(calls) == 1:
                return copy.deepcopy(bad)
            current = previous_draft(prompt)
            self.assertEqual(current, bad)
            context = conflict_map(prompt)
            self.assertIn("non_english_query_beat_ids", context)
            self.assertIn("duplicate_viewer_intent_pairs", context)
            self.assertIn("stationery", context["overused_families"])
            self.assertEqual(context["post_hook_weak_beat_ids"], ["b6"])
            for index in (1, 2, 3, 4, 5):
                current["visual_story"]["beats"][index] = copy.deepcopy(good["visual_story"]["beats"][index])
            return current

        router = ProviderRouter((adapter("mistral", invoke, "planning"),))
        result = router.route(stage="planning", prompt=pipeline._planning_prompt(_brief("short")),
                              max_tokens=3000, validator=lambda v: pipeline._validate_plan_for_brief(v, _brief("short")))
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["practical_action_ar"], good["practical_action_ar"])
        self.assertEqual(result["visual_story"]["beats"][0]["viewer_intent"], good["visual_story"]["beats"][0]["viewer_intent"])
        self.assertEqual(len(result["visual_story"]["beats"]), 7)

    def test_provider_fallback_sees_latest_draft_all_intent_pairs_and_prior_query_fixes(self):
        bad = language_and_intent_conflict()
        good = healthy_plan()
        calls = []
        latest = None

        def mistral(prompt, *_):
            nonlocal latest
            calls.append("mistral")
            current = copy.deepcopy(bad) if len(calls) == 1 else previous_draft(prompt)
            if len(calls) == 2:
                self.assertEqual(conflict_map(prompt)["non_english_query_beat_ids"]["stock_query_en"], ["b4"])
                current["visual_story"]["beats"][1].pop("stock_query_alt_en")
            if len(calls) == 3:
                current["visual_story"]["beats"][3]["stock_query_en"] = good["visual_story"]["beats"][3]["stock_query_en"]
            latest = copy.deepcopy(current)
            return current

        def gemini(prompt, *_):
            calls.append("gemini_flash_lite")
            self.assertEqual(previous_draft(prompt), latest)
            self.assertEqual(conflict_map(prompt)["duplicate_viewer_intent_pairs"], [["b1", "b3"], ["b2", "b5"]])
            self.assertNotIn("non_english_query_beat_ids", conflict_map(prompt))
            self.assertIn("stock_query_alt_en must stay English", prompt)
            self.assertIn("viewer_intent values must add new information", prompt)
            self.assertEqual(prompt.count("PREVIOUS_PLANNING_JSON:\n"), 1)
            current = previous_draft(prompt)
            for index in (2, 4):
                current["visual_story"]["beats"][index] = copy.deepcopy(good["visual_story"]["beats"][index])
            return current

        router = ProviderRouter((adapter("mistral", mistral, "planning"), adapter("gemini_flash_lite", gemini, "planning")))
        router.route(stage="planning", prompt=pipeline._planning_prompt(_brief("short")), max_tokens=3000,
                     validator=lambda v: pipeline._validate_plan_for_brief(v, _brief("short")))
        self.assertEqual(calls, ["mistral", "mistral", "mistral", "gemini_flash_lite"])
        self.assertNotIn("بجوار المهمة", json.dumps(router.events, ensure_ascii=False))

    def test_query_and_intent_diagnostics_are_bounded_and_do_not_log_authored_text(self):
        error = ValueError("invalid planning")
        ids = [f"b{i}" for i in range(100)]
        pairs = [[f"b{i}", f"b{i+1}"] for i in range(100)]
        error.planning_repair_context = {
            "non_english_query_beat_ids": {"stock_query_en": ids, "private query": ["SECRET"]},
            "duplicate_viewer_intent_pairs": pairs,
            "viewer_intent": "SECRET_AUTHORED_INTENT",
        }
        diagnostic = _safe_mistral_planning_raw_diagnostic(json.dumps({"private": "SECRET_QUERY"}), error)
        context = diagnostic["repair_context"]
        self.assertEqual(len(context["non_english_query_beat_ids"]["stock_query_en"]), MAX_PLANNING_REPAIR_BEATS)
        self.assertEqual(len(context["duplicate_viewer_intent_pairs"]), MAX_PLANNING_REPAIR_BEATS)
        self.assertEqual(list(context["non_english_query_beat_ids"]), ["stock_query_en"])
        self.assertNotIn("SECRET", json.dumps(diagnostic))
        error.planning_repair_context = {
            "non_english_query_beat_ids": {"stock_query_alt_en": ["bad id", "ب1", "b2", "b2"]},
            "duplicate_viewer_intent_pairs": [["b1", "b1"], ["b1", "bad id"], ["b1", "b2"]],
        }
        context = _safe_mistral_planning_raw_diagnostic("{}", error)["repair_context"]
        self.assertEqual(context, {"non_english_query_beat_ids": {"stock_query_alt_en": ["b2"]},
                                   "duplicate_viewer_intent_pairs": [["b1", "b2"]]})

    def test_ignored_query_and_intent_feedback_still_fails_with_the_same_call_budget(self):
        bad = language_and_intent_conflict()
        calls = []

        def invoke(prompt, *_):
            calls.append(prompt)
            return copy.deepcopy(bad)

        router = ProviderRouter((adapter("mistral", invoke, "planning"), adapter("gemini_flash_lite", invoke, "planning")))
        with self.assertRaisesRegex(RuntimeError, "planning exhausted bounded provider route"):
            router.route(stage="planning", prompt="BASE", max_tokens=3000,
                         validator=lambda v: pipeline._validate_plan_for_brief(v, _brief("short")))
        self.assertEqual(len(calls), 4)
        for prompt in calls[1:]:
            self.assertEqual(previous_draft(prompt), bad)
            self.assertIn("non_english_query_beat_ids", conflict_map(prompt))
            self.assertIn("duplicate_viewer_intent_pairs", conflict_map(prompt))

    def test_query_and_intent_feedback_respects_the_admitted_prompt_ceiling(self):
        bad = language_and_intent_conflict()
        error = rejected(bad)
        plain = _mistral_planning_validator_retry_prompt("BASE", error)
        cap = len(plain.encode("utf-8")) + 5
        prompt = _mistral_planning_validator_retry_prompt("BASE", error, candidate=bad, max_prompt_bytes=cap)
        self.assertEqual(prompt, plain)
        self.assertEqual(conflict_map(prompt)["duplicate_viewer_intent_pairs"], [["b1", "b3"], ["b2", "b5"]])
        self.assertLessEqual(len(prompt.encode("utf-8")), cap)
        self.assertLessEqual(cap, MAX_PROMPT_BYTES)
        self.assertIsNone(_mistral_planning_validator_retry_prompt("BASE", error, candidate=bad, max_prompt_bytes=4))


class AllTemplatesQueryIntentFeedbackTests(unittest.TestCase):
    def check_template(self, fmt, template):
        brief = _brief(fmt)
        profile = (pipeline.select_short_template(brief) if fmt == "short"
                   else pipeline._select_longform_narrative_profile(brief))
        key = "template" if fmt == "short" else "narrative_format"
        profile[key] = template
        selector = "select_short_template" if fmt == "short" else "_select_longform_narrative_profile"
        good = healthy_plan(fmt)
        bad = language_and_intent_conflict(fmt)
        calls = []

        def invoke(prompt, *_):
            calls.append(prompt)
            if len(calls) == 1:
                return copy.deepcopy(bad)
            current = previous_draft(prompt)
            self.assertEqual(current, bad)
            self.assertEqual(conflict_map(prompt)["duplicate_viewer_intent_pairs"], [["b1", "b3"], ["b2", "b5"]])
            self.assertIn("cut applies only to Short", prompt)
            self.assertNotIn("Every Short beat", prompt)
            for index in (1, 2, 3, 4):
                current["visual_story"]["beats"][index] = copy.deepcopy(good["visual_story"]["beats"][index])
            return current

        router = ProviderRouter((adapter("mistral", invoke, "planning"),))
        with patch.object(pipeline, selector, return_value=profile):
            result = router.route(stage="planning", prompt=pipeline._planning_prompt(brief), max_tokens=3000,
                                  validator=lambda v: pipeline._validate_plan_for_brief(v, brief))
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["short_template" if fmt == "short" else "narrative_format"], template)
        self.assertEqual([s["id"] for s in result["sections"]], [s["id"] for s in good["sections"]])
        self.assertEqual(len(result["visual_story"]["beats"]), len(good["visual_story"]["beats"]))
        self.assertEqual(result["visual_story"]["beats"][0]["viewer_intent"], good["visual_story"]["beats"][0]["viewer_intent"])


def template_case(fmt, template):
    def test(self):
        self.check_template(fmt, template)
    return test


for _fmt, _templates in (("short", TEMPLATE_ORDER), ("film", sorted(LONGFORM_NARRATIVE_FORMATS)), ("podcast", ("dialogue_qa",))):
    for _template in _templates:
        setattr(AllTemplatesQueryIntentFeedbackTests, f"test_{_fmt}_{_template}", template_case(_fmt, _template))


if __name__ == "__main__":
    unittest.main()

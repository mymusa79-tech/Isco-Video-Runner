from __future__ import annotations

import copy
import json
import unittest
from unittest.mock import patch

from clean_v2 import pipeline, providers
from clean_v2.contracts import LONGFORM_NARRATIVE_FORMATS
from clean_v2.short_format import TEMPLATE_ORDER
from clean_v2.visual_story import SHORT_BEAT_SECTION_IDS, visual_story_repair_context
from scripts.test_clean_v2_unified_visual_story import _brief
from scripts.test_planning_query_intent_feedback import conflict_map
from scripts.test_planning_visual_repair_feedback import healthy_plan, rejected, scene
from scripts.test_run182_planning_repair_context import previous_draft


def run186_failure_shape():
    """Synthetic draft matching the logged errors; Run186 did not save raw text."""
    value = healthy_plan()
    queries = {
        2: "hands arranging two paper cards beside an unopened notebook",
        3: "hands crossing out two paper options leaving one card",
        4: "hands crossing one checklist line on paper",
        5: "hands sorting paper task cards into one chosen stack",
        6: "hands selecting one priority card then writing next priority in notebook",
    }
    for index, query in queries.items():
        scene(value, index, query)
        beat = value["visual_story"]["beats"][index]
        beat["viewer_intent"] = f"يفهم المشاهد تفصيل الدفتر {index}"
        beat["meaning_target"] = f"تفصيل مرئي مختلف في الدفتر {index}"
        beat["display_text_ar"] = f"تفصيل الدفتر {index}"
    # One possible invalid layout consistent with the s3-overflow rejection,
    # not a claim about section assignments absent from the actual artifact.
    for beat, section in zip(value["visual_story"]["beats"], ("s1", "s2", "s3", "s3", "s3", "s3", "s3")):
        beat["section_id"] = section
    return value


class PlanningLayoutFamilyRepairTests(unittest.TestCase):
    def test_real_validator_keeps_primary_error_and_names_concurrent_repairs(self):
        value = run186_failure_shape()
        original = copy.deepcopy(value)
        error = rejected(value)
        self.assertEqual(str(error), "visual_story section s3 exceeds 3 beats")
        context = error.planning_repair_context
        self.assertEqual(context["section_beat_ids"], {"s1": ["b1"], "s2": ["b2"], "s3": ["b3", "b4", "b5", "b6", "b7"]})
        self.assertEqual(context["misplaced_section_beat_ids"], {"b2": "s1", "b3": "s1", "b4": "s2", "b5": "s2"})
        self.assertEqual(context["family_beat_ids"]["stationery"], ["b1", "b3", "b4", "b5", "b6", "b7"])
        self.assertEqual(context["family_replacement_beat_ids"], {"stationery": ["b4", "b5", "b6", "b7"]})
        self.assertEqual(value, original)

    def test_first_overflow_feedback_repairs_layout_and_literal_prop_lock_together(self):
        bad = run186_failure_shape()
        prompt = providers._mistral_planning_validator_retry_prompt("BASE", rejected(bad), candidate=bad)
        self.assertEqual(previous_draft(prompt), bad)
        self.assertIn("do not preserve invalid assignments", prompt)
        self.assertNotIn("Preserve each beat's section_id", prompt)
        self.assertIn("not yet approved Script truth", prompt)
        self.assertIn("re-author it together with that new evidence", prompt)
        self.assertIn("section purposes, story arc, hook_tension, payoff_answer", prompt)
        self.assertIn("primary AND alternate queries", prompt)
        self.assertEqual(conflict_map(prompt)["family_replacement_beat_ids"]["stationery"], ["b4", "b5", "b6", "b7"])

    def test_valid_prop_meaning_is_not_globally_discarded_by_family_feedback(self):
        bad = run186_failure_shape()
        for beat, section in zip(bad["visual_story"]["beats"], SHORT_BEAT_SECTION_IDS):
            beat["section_id"] = section
        error = rejected(bad)
        self.assertIn("family exceeds two beats: stationery", str(error))
        prompt = providers._mistral_planning_validator_retry_prompt("BASE", error, candidate=bad)
        self.assertIn("when viewer_intent, meaning_target", prompt)
        self.assertIn("tied to the rejected prop", prompt)
        self.assertIn("Preserve valid assignments, valid viewer_intent/meaning_target", prompt)
        self.assertIn("practical_action_ar", prompt)

    def test_seven_beat_layout_diagnostics_also_find_non_overflowing_wrong_cut(self):
        value = healthy_plan()
        value["_short_visual_diversity_contract"] = "v1_max2"
        value["visual_story"]["beats"][2]["section_id"] = "s2"
        context = visual_story_repair_context(value["visual_story"], value)
        self.assertNotIn("section_beat_ids", context)
        self.assertEqual(context["misplaced_section_beat_ids"], {"b3": "s1"})

    def test_diagnostics_do_not_guess_layout_for_incomplete_or_duplicate_beats(self):
        for shape in ("missing", "duplicate"):
            with self.subTest(shape=shape):
                value = run186_failure_shape()
                value["_short_visual_diversity_contract"] = "v1_max2"
                if shape == "missing":
                    value["visual_story"]["beats"].pop()
                else:
                    value["visual_story"]["beats"][1]["id"] = "b1"
                context = visual_story_repair_context(value["visual_story"], value)
                self.assertNotIn("short_beat_section_ids", context)
                self.assertNotIn("misplaced_section_beat_ids", context)

    def test_new_diagnostic_maps_are_bounded_and_reject_authored_text(self):
        error = ValueError("rejected")
        error.planning_repair_context = {
            "section_beat_ids": {"s1": ["b1"], "private authored section": ["b2"], "s2": []},
            "section_beat_limit": 99,
            "short_beat_section_ids": {**{f"b{i}": "s1" for i in range(100)}, "private words": "s1"},
            "misplaced_section_beat_ids": {"b2": "private words", "b3": "s4", "b4": "s2"},
            "family_replacement_beat_ids": {"stationery": ["b4", "private authored text"], "private": ["b2"]},
        }
        diagnostic = providers._safe_mistral_planning_raw_diagnostic('{"title":"SECRET_TITLE"}', error)
        context = diagnostic["repair_context"]
        self.assertEqual(context["section_beat_ids"], {"s1": ["b1"], "s2": []})
        self.assertNotIn("section_beat_limit", context)
        self.assertEqual(len(context["short_beat_section_ids"]), 7)
        self.assertEqual(context["misplaced_section_beat_ids"], {"b4": "s2"})
        self.assertEqual(context["family_replacement_beat_ids"], {"stationery": ["b4"]})
        self.assertNotIn("private", json.dumps(diagnostic))
        self.assertNotIn("SECRET_TITLE", json.dumps(diagnostic))

    def test_long_forms_keep_three_nonadjacent_uses_and_no_short_cut(self):
        for fmt in ("film", "podcast"):
            with self.subTest(fmt=fmt):
                value = healthy_plan(fmt)
                scene(value, 2, "hands placing one paper card beside a finished object")
                scene(value, 3, "single completed progress marker beside next step object")
                scene(value, 4, "closed notebook beside one completed physical task")
                result = pipeline._validate_plan_for_brief(value, _brief(fmt))
                context = visual_story_repair_context(result["visual_story"], result)
                self.assertEqual(len(context["family_beat_ids"]["stationery"]), 3)
                for key in ("family_replacement_beat_ids", "visual_family_limit", "short_beat_section_ids"):
                    self.assertNotIn(key, context)
                schema = providers._mistral_planning_response_schema(pipeline._planning_prompt(_brief(fmt)))
                self.assertNotIn("prefixItems", schema["properties"]["sections"])
                self.assertNotIn("prefixItems", schema["properties"]["visual_story"]["properties"]["beats"])

    def test_all_provider_schema_shapes_keep_existing_compatibility(self):
        for fmt in ("short", "film", "podcast"):
            with self.subTest(fmt=fmt):
                prompt = pipeline._planning_prompt(_brief(fmt))
                groq = providers._groq_planning_response_schema(prompt)
                gemini = providers._gemini_planning_response_schema(prompt)
                self.assertNotIn("prefixItems", json.dumps(groq))
                self.assertNotIn("prefixItems", json.dumps(gemini))
                beat = groq["properties"]["visual_story"]["properties"]["beats"]["items"]
                self.assertEqual(set(beat["required"]), set(beat["properties"]))
                self.assertEqual(beat["properties"]["stock_query_alt_en"]["type"], ["string", "null"])
                self.assertTrue(gemini["properties"]["visual_story"]["properties"]["beats"]["items"]["additionalProperties"])

    def test_mistral_first_wire_call_carries_fixed_short_slots_and_all_proof_fields(self):
        prompt = pipeline._planning_prompt(_brief("short"))
        with patch.object(providers.mistral_executor, "mistral_executor_json", return_value=healthy_plan()) as called:
            providers._mistral_call(prompt, 3000, "planning")
        name, schema = called.call_args.kwargs["response_schema"]
        self.assertEqual(name, "planning")
        sections = schema["properties"]["sections"]
        self.assertEqual([s["properties"]["id"]["const"] for s in sections["prefixItems"]], ["s1", "s2", "s3"])
        beats = schema["properties"]["visual_story"]["properties"]["beats"]
        self.assertEqual(beats["minItems"], 7)
        self.assertEqual(beats["maxItems"], 7)
        self.assertEqual(tuple(b["properties"]["section_id"]["const"] for b in beats["prefixItems"]), SHORT_BEAT_SECTION_IDS)
        self.assertEqual([b["properties"]["role"]["const"] for b in beats["prefixItems"]], ["hook", "body", "body", "body", "body", "body", "payoff"])
        for slot in beats["prefixItems"]:
            self.assertFalse(slot["additionalProperties"])
            self.assertEqual(set(slot["required"]), set(beats["items"]["required"]))
            self.assertIn("semantic_must_have", slot["properties"])
            self.assertIn("semantic_should_avoid", slot["properties"])
        self.assertEqual(schema["properties"]["cta"]["const"], "")

    def test_ignored_repairs_keep_existing_attempt_limit_and_fallback(self):
        calls = []
        def invoke_bad(*args):
            calls.append("mistral")
            return run186_failure_shape()
        def invoke_good(*args):
            calls.append("gemini_flash_lite")
            return healthy_plan()
        adapters = tuple(providers.ProviderAdapter(name, invoke, stages=frozenset({"planning"}), accepts_stage=True)
                         for name, invoke in (("mistral", invoke_bad), ("gemini_flash_lite", invoke_good)))
        router = providers.ProviderRouter(adapters)
        with patch("builtins.print"):
            result = router.route(stage="planning", prompt="BASE", max_tokens=3000,
                                  validator=lambda v: pipeline._validate_plan_for_brief(v, _brief("short")))
        self.assertEqual(calls, ["mistral", "mistral", "mistral", "gemini_flash_lite"])
        self.assertEqual(len(result["visual_story"]["beats"]), 7)
        self.assertTrue(all(e["reason"] == "mistral_planning_validator_retry" for e in router.events if e["result"] == "retrying"))


class AllTemplatesLayoutFamilyRepairTests(unittest.TestCase):
    def check_template(self, fmt, template):
        brief = _brief(fmt)
        selector = "select_short_template" if fmt == "short" else "_select_longform_narrative_profile"
        profile = getattr(pipeline, selector)(brief)
        profile["template" if fmt == "short" else "narrative_format"] = template
        bad = run186_failure_shape() if fmt == "short" else healthy_plan(fmt)
        if fmt != "short":
            scene(bad, 1, "person sitting at desk using laptop")
            bad["sections"][1]["visual_query_alt_en"] = "person scrolling phone while sitting at desk"
            scene(bad, 2, "hands crossing out two paper options leaving one card")
        good = healthy_plan(fmt)
        # Keep the two already valid nonadjacent stationery depictions in Short.
        if fmt == "short":
            good["visual_story"]["beats"][2] = copy.deepcopy(bad["visual_story"]["beats"][2])
            good["visual_story"]["beats"][2]["section_id"] = "s1"
        else:
            scene(good, 3, "single completed progress marker beside next step object")
        calls = []
        corrected = None
        def invoke(prompt, _tokens, _stage):
            nonlocal corrected
            calls.append(prompt)
            if len(calls) == 1:
                return copy.deepcopy(bad)
            corrected = previous_draft(prompt)
            self.assertEqual(corrected, bad)
            context = conflict_map(prompt)
            self.assertIn("family_replacement_beat_ids", context)
            if fmt == "short":
                self.assertEqual(context["misplaced_section_beat_ids"]["b2"], "s1")
                for beat in corrected["visual_story"]["beats"]:
                    beat["section_id"] = context["short_beat_section_ids"][beat["id"]]
                indexes = (3, 4, 5, 6)
            else:
                self.assertNotIn("short_beat_section_ids", context)
                indexes = (1, 3)
            for index in indexes:
                corrected["visual_story"]["beats"][index] = copy.deepcopy(good["visual_story"]["beats"][index])
            return corrected
        router = providers.ProviderRouter((providers.ProviderAdapter("mistral", invoke, stages=frozenset({"planning"}), accepts_stage=True),))
        with patch.object(pipeline, selector, return_value=profile):
            result = router.route(stage="planning", prompt=pipeline._planning_prompt(brief), max_tokens=3000,
                                  validator=lambda v: pipeline._validate_plan_for_brief(v, brief))
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["short_template" if fmt == "short" else "narrative_format"], template)
        self.assertEqual(corrected["sections"], bad["sections"])
        for key in ("story_arc", "retention_thread"):
            self.assertEqual(corrected["visual_story"][key], bad["visual_story"][key])
        if fmt == "short":
            self.assertEqual(result["practical_action_ar"], bad["practical_action_ar"])
            self.assertEqual(tuple(b["section_id"] for b in result["visual_story"]["beats"]), SHORT_BEAT_SECTION_IDS)
        self.assertTrue(all(len(p.encode("utf-8")) <= providers.MAX_PROMPT_BYTES for p in calls))


def template_case(fmt, template):
    def test(self):
        self.check_template(fmt, template)
    return test


for _fmt, _templates in (("short", TEMPLATE_ORDER), ("film", sorted(LONGFORM_NARRATIVE_FORMATS)), ("podcast", ("dialogue_qa",))):
    for _template in _templates:
        setattr(AllTemplatesLayoutFamilyRepairTests, f"test_{_fmt}_{_template}", template_case(_fmt, _template))


if __name__ == "__main__":
    unittest.main()

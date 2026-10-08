from __future__ import annotations

import copy
import json
import unittest
from unittest.mock import patch

from clean_v2 import pipeline
from clean_v2.contracts import LONGFORM_NARRATIVE_FORMATS, validate_script
from clean_v2.providers import MAX_PROMPT_BYTES, ProviderAdapter, ProviderRouter, ProviderWireFailure
from clean_v2.short_format import TEMPLATE_ORDER, ShortFormatError
from scripts.test_clean_v2_unified_visual_story import _brief
from scripts.test_planning_visual_repair_feedback import combined_conflict, healthy_plan
from scripts.test_text_audit_all_templates_closure import scenario


def adapter(name, invoke, stage, **limits):
    return ProviderAdapter(name, invoke, stages=frozenset({stage}), accepts_stage=True, **limits)


def rejected_text(prompt, stage="script"):
    label = "REJECTED_SCRIPT_PATCH_JSON" if stage == "script_patch" else "REJECTED_SCRIPT_JSON"
    tail = prompt.split(f"\n{label}:\n", 1)[1]
    return json.JSONDecoder().raw_decode(tail)[0]


def previous_draft(prompt):
    return json.JSONDecoder().raw_decode(prompt.split("\n\nPREVIOUS_PLANNING_JSON:\n", 1)[1])[0]


def patch_scenario(fmt, template):
    brief, plan, script = scenario(fmt, template)
    script["sections"][0]["narration"] = "لماذا تقيس تقدمك بسرعة شخص آخر رغم اختلاف مسارك عنه؟"
    script["sections"][1]["narration"] = "المقارنة تستبدل معيار تقدمك بصورة الآخرين، فيغيب أثر الطريق الذي قطعته."
    script["sections"][2]["narration"] = "مسارك الزمني خاص بك، وقيمتك أوسع من ترتيب ظهورك في الصورة."
    if fmt == "short":
        plan["practical_action_ar"] = "اكتب إنجازًا شخصيًا واحدًا اليوم."
        script["sections"][2]["s3_payoff"] = script["sections"][2]["narration"]
    script = validate_script(script, plan)
    if fmt == "short":
        pipeline.materialize_short_s3(script)
    revision_note = "content_depth:s2/s3 — explain the comparison mechanism and its earned result."
    patches = {"patches": [
        {"section_id": "s2", "find": "فيغيب أثر الطريق الذي قطعته", "replace": "فيغيب أثر تقدمك بين الأمس واليوم"},
        {"section_id": "s3", "find": "وقيمتك أوسع من ترتيب ظهورك في الصورة", "replace": "وتقدمك يتضح من تغيرك أنت عبر الزمن"},
    ]}
    return brief, plan, script, revision_note, patches


class ProviderRejectionFeedbackTests(unittest.TestCase):
    def test_planning_fallback_keeps_latest_draft_semantic_fix_and_family_map(self):
        calls = []
        latest = None

        def mistral(prompt, *_):
            nonlocal latest
            calls.append("mistral")
            candidate = combined_conflict() if len(calls) == 1 else previous_draft(prompt)
            if len(calls) > 1:
                candidate["visual_story"]["beats"][5] = healthy_plan()["visual_story"]["beats"][5]
            latest = copy.deepcopy(candidate)
            return candidate

        def gemini(prompt, *_):
            calls.append("gemini_flash_lite")
            self.assertEqual(previous_draft(prompt), latest)
            self.assertIn("PLANNING_PROVIDER_FALLBACK_RECOVERY", prompt)
            self.assertIn("post-hook semantic drop", prompt)
            self.assertIn('"stationery":["b1","b3","b4"]', prompt)
            self.assertEqual(prompt.count("PREVIOUS_PLANNING_JSON:\n"), 1)
            return healthy_plan()

        router = ProviderRouter((adapter("mistral", mistral, "planning"), adapter("gemini_flash_lite", gemini, "planning")))
        with patch("builtins.print"):
            result = router.route(stage="planning", prompt="LOCKED BRIEF", max_tokens=3000,
                                  validator=lambda v: pipeline._validate_plan_for_brief(v, _brief("short")))
        self.assertEqual(calls, ["mistral"] * 3 + ["gemini_flash_lite"])
        self.assertEqual(len(result["visual_story"]["beats"]), 7)

    def test_early_generic_action_rejection_also_reports_hidden_visual_defects(self):
        candidate = combined_conflict()
        candidate["practical_action_ar"] = "اختر مهمة الآن."
        with self.assertRaisesRegex(ShortFormatError, "short_practical_action_too_generic_for_topic") as failure:
            pipeline._validate_plan_for_brief(candidate, _brief("short"))
        self.assertEqual(failure.exception.planning_repair_context["post_hook_weak_beat_ids"], ["b6"])
        self.assertEqual(failure.exception.planning_repair_context["overused_families"], ["stationery"])
        calls = []

        def first(*_):
            calls.append("gemini_flash_lite")
            return candidate

        def second(prompt, *_):
            calls.append("mistral")
            self.assertEqual(previous_draft(prompt), candidate)
            self.assertIn("short_practical_action_too_generic_for_topic", prompt)
            self.assertIn("Fix ALL listed visual conflicts together", prompt)
            self.assertIn('"overused_families":["stationery"]', prompt)
            return healthy_plan()

        router = ProviderRouter((adapter("gemini_flash_lite", first, "planning"), adapter("mistral", second, "planning")))
        router.route(stage="planning", prompt="BASE", max_tokens=3000,
                     validator=lambda v: pipeline._validate_plan_for_brief(v, _brief("short")))
        self.assertEqual(calls, ["gemini_flash_lite", "mistral"])

    def test_retry_transport_failure_retains_last_rejected_draft_for_fallback(self):
        candidate = combined_conflict()
        calls = []
        def first(prompt, *_):
            calls.append("mistral")
            if len(calls) == 2:
                raise ProviderWireFailure("http_503", http_status=503)
            return candidate
        def second(prompt, *_):
            calls.append("gemini_flash_lite")
            self.assertEqual(previous_draft(prompt), candidate)
            return healthy_plan()
        router = ProviderRouter((adapter("mistral", first, "planning"), adapter("gemini_flash_lite", second, "planning")))
        router.route(stage="planning", prompt="BASE", max_tokens=3000,
                     validator=lambda v: pipeline._validate_plan_for_brief(v, _brief("short")))
        self.assertEqual(calls, ["mistral", "mistral", "gemini_flash_lite"])

    def test_text_same_provider_correction_sees_the_actual_failed_script(self):
        brief, plan, script, _, _ = patch_scenario("short", "why_reframe")
        rejected = copy.deepcopy(script)
        rejected["sections"][0]["narration"] = " ".join(["هل"] + ["تفقد"] * 22) + "؟"
        # Obtain the real hook rejection rather than teaching a fake validator
        # the correction's expected success condition.
        with self.assertRaisesRegex(ShortFormatError, "short_hook_too_long"):
            pipeline._validate_script_for_brief(rejected, plan, brief)
        calls = []
        def invoke(prompt, *_):
            calls.append(prompt)
            if len(calls) == 1:
                return rejected
            self.assertEqual(rejected_text(prompt), rejected)
            self.assertIn("hard maximum 18", prompt)
            self.assertIn("LOCKED_PLAN", prompt)
            self.assertEqual(prompt.count("REJECTED_SCRIPT_JSON:\n"), 1)
            return script
        router = ProviderRouter((adapter("mistral", invoke, "script"),))
        result = router.route(stage="script", prompt="LOCKED_PLAN:\n" + json.dumps(plan), max_tokens=2000,
                              validator=lambda v: pipeline._validate_script_for_brief(v, plan, brief))
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["sections"][-1]["s3_locked_action"], plan["practical_action_ar"])

    def test_text_fallback_and_correction_do_not_stack_stale_drafts(self):
        _, plan, script, _, _ = patch_scenario("film", "micro_story")
        bad = copy.deepcopy(script)
        bad["sections"].reverse()
        nearly = copy.deepcopy(script)
        nearly["sections"][0]["narration"] = " ".join(["هل"] + ["تفقد"] * 22) + "؟"
        calls = []
        def gemini(*_):
            calls.append("gemini_flash_lite")
            return bad
        def mistral(prompt, *_):
            calls.append("mistral")
            if len(calls) == 2:
                self.assertEqual(rejected_text(prompt), bad)
                self.assertIn("section ids/order must match plan exactly", prompt)
                return nearly
            self.assertEqual(rejected_text(prompt), nearly)
            self.assertEqual(prompt.count("REJECTED_SCRIPT_JSON:\n"), 1)
            self.assertNotIn("TEXT_PROVIDER_FALLBACK_RECOVERY", prompt)
            return script
        def validator(value):
            normalized = validate_script(value, plan)
            pipeline.validate_short_hook_contract(normalized)
            return normalized
        router = ProviderRouter((adapter("gemini_flash_lite", gemini, "script"), adapter("mistral", mistral, "script")))
        router.route(stage="script", prompt="BASE", max_tokens=3000, validator=validator)
        self.assertEqual(calls, ["gemini_flash_lite", "mistral", "mistral"])

    def test_feedback_respects_provider_limit_and_omits_a_whole_oversized_snapshot(self):
        _, plan, script, _, _ = patch_scenario("film", "micro_story")
        bad = copy.deepcopy(script)
        bad["sections"].reverse()
        bad["large_unused_field"] = "س" * MAX_PROMPT_BYTES
        prompts = []
        def invoke(prompt, *_):
            prompts.append(prompt)
            self.assertLessEqual(len(prompt.encode("utf-8")), 2000)
            self.assertNotIn("REJECTED_SCRIPT_JSON:\n", prompt)
            self.assertIn("section ids/order", prompt)
            return script
        router = ProviderRouter((adapter("gemini_flash_lite", lambda *_: bad, "script"),
                                 adapter("groq", invoke, "script", max_prompt_utf8_bytes=2000)))
        router.route(stage="script", prompt="BASE", max_tokens=3000, validator=lambda v: validate_script(v, plan))
        self.assertEqual(len(prompts), 1)
        tight = []
        router = ProviderRouter((adapter("gemini_flash_lite", lambda *_: bad, "script"),
                                 adapter("groq", lambda p, *_: tight.append(p) or script, "script", max_prompt_utf8_bytes=4)))
        router.route(stage="script", prompt="BASE", max_tokens=3000, validator=lambda v: validate_script(v, plan))
        self.assertEqual(tight, ["BASE"])

    def test_unrelated_stages_and_unknown_runtime_errors_keep_original_prompt(self):
        for stage, error in (("other_stage", ValueError("invalid output")), ("script", RuntimeError("database unavailable"))):
            with self.subTest(stage=stage):
                prompts = []
                def validator(value):
                    if value.get("bad"):
                        raise error
                    return value
                router = ProviderRouter((adapter("first", lambda *_: {"bad": True}, stage),
                                         adapter("next", lambda p, *_: prompts.append(p) or {"ok": True}, stage)))
                router.route(stage=stage, prompt="BASE", max_tokens=1000, validator=validator)
                self.assertEqual(prompts, ["BASE"])

    def test_rejection_feedback_is_local_to_one_route_and_not_logged_as_text(self):
        private = "PRIVATE_REJECTED_NARRATION"
        calls = []
        def validator(value):
            if "private" in value:
                raise ValueError("wrong section structure")
            return value
        router = ProviderRouter((adapter("first", lambda *_: {"private": private}, "script"),
                                 adapter("next", lambda p, *_: calls.append(p) or {"ok": True}, "script")))
        router.route(stage="script", prompt="BASE", max_tokens=1000, validator=validator)
        self.assertIn(private, calls[0])
        self.assertNotIn(private, json.dumps(router.events))
        router.adapters = (adapter("next", lambda p, *_: calls.append(p) or {"ok": True}, "script"),)
        router.route(stage="script", prompt="FRESH", max_tokens=1000, validator=validator)
        self.assertEqual(calls[1], "FRESH")

    def test_terminal_locked_action_rejection_on_retry_stops_provider_fallback(self):
        brief, plan, script, note, good = patch_scenario("short", "why_reframe")
        partial = {"patches": [good["patches"][0]]}
        forbidden = {"patches": [{"section_id": "s3", "find": plan["practical_action_ar"],
                                   "replace": "اكتب هدفًا جديدًا الآن."}]}
        calls = []
        def mistral(*_):
            calls.append("mistral")
            return partial if len(calls) == 1 else forbidden
        router = ProviderRouter((adapter("mistral", mistral, "script_patch"),
                                 adapter("gemini_flash_lite", lambda *_: calls.append("gemini_flash_lite") or good, "script_patch")))
        with self.assertRaisesRegex(ValueError, "cannot change Planning-owned practical_action_ar"):
            router.route(stage="script_patch", prompt="BASE", max_tokens=1200,
                         validator=lambda v: pipeline._validate_and_apply_script_patches(
                             v, plan=plan, original_script=script, identity={}, cta_plan={}, revision_note=note,
                             allowed_section_ids=("s2", "s3"), required_changed_section_ids=("s2", "s3"), is_short_format=True))
        self.assertEqual(calls, ["mistral", "mistral"])

    def test_explicitly_authorized_action_repair_still_counts_as_a_real_s3_change(self):
        _, plan, script, note, _ = patch_scenario("short", "why_reframe")
        action = "اكتب إنجازًا شخصيًا واحدًا هذا الأسبوع."
        result = pipeline._validate_and_apply_script_patches(
            {"patches": [{"section_id": "s3", "find": plan["practical_action_ar"], "replace": action}]},
            plan=plan, original_script=script, identity={}, cta_plan={}, revision_note=note,
            allowed_section_ids=("s3",), required_changed_section_ids=("s3",),
            is_short_format=True, allow_short_locked_action_repair=True,
        )
        self.assertEqual(result["sections"][-1]["s3_locked_action"], action)
        self.assertEqual(result["sections"][-1]["s3_payoff"], script["sections"][-1]["s3_payoff"])

    def test_legacy_materialized_action_is_not_a_false_s3_semantic_change(self):
        _, plan, script, note, good = patch_scenario("short", "why_reframe")
        script["sections"][-1].pop("s3_payoff")
        with self.assertRaisesRegex(ValueError, "explicitly flagged section: s3"):
            pipeline._validate_and_apply_script_patches(
                {"patches": [good["patches"][0]]}, plan=plan, original_script=script,
                identity={}, cta_plan={}, revision_note=note, allowed_section_ids=("s2", "s3"),
                required_changed_section_ids=("s2", "s3"), is_short_format=True,
            )


class AllTemplatesPatchFeedbackTests(unittest.TestCase):
    def check_patch(self, fmt, template):
        brief, plan, script, note, good = patch_scenario(fmt, template)
        original = copy.deepcopy(script)
        required = pipeline._required_semantic_repair_section_ids(script, note)
        self.assertEqual(required, ("s2", "s3"))
        prompt = pipeline._tone_repair_prompt(brief=brief, plan=plan, script=script, identity={}, cta_plan={}, revision_note=note)
        shape = pipeline._editorial_shape_contract_context(brief, plan)
        # The first candidate omits s3. The sole correction repairs s3's exact
        # find but leaves s2 unchanged. The existing fallback must retain the
        # latest real patch, all original flags and both required section ids.
        candidates = [{"patches": [good["patches"][0]]}, {"patches": [good["patches"][1]]}]
        calls = []
        def mistral(p, *_):
            calls.append("mistral")
            if len(calls) == 2:
                self.assertEqual(rejected_text(p, "script_patch"), candidates[0])
                self.assertIn("Missing required section ids: s3", p)
                self.assertIn(shape, p)
                self.assertIn(note, p)
                self.assertIn("not from an unapplied replacement", p)
            return copy.deepcopy(candidates[len(calls) - 1])
        def gemini(p, *_):
            calls.append("gemini_flash_lite")
            self.assertEqual(rejected_text(p, "script_patch"), candidates[1])
            self.assertEqual(p.count("REJECTED_SCRIPT_PATCH_JSON:\n"), 1)
            self.assertIn("Missing required section ids: s2", p)
            self.assertIn("Missing required section ids: s3", calls_prompt[0])
            self.assertIn("not from an unapplied replacement", p)
            self.assertIn(shape, p)
            self.assertIn(note, p)
            return copy.deepcopy(good)
        calls_prompt = []
        def capture_mistral(p, *args):
            if len(calls) == 1:
                calls_prompt.append(p)
            return mistral(p, *args)
        validator = lambda v: pipeline._validate_and_apply_script_patches(
            v, plan=plan, original_script=script, identity={}, cta_plan={}, revision_note=note,
            allowed_section_ids=("s2", "s3"), required_changed_section_ids=required, is_short_format=fmt == "short")
        with self.assertRaisesRegex(ValueError, "explicitly flagged section: s3"):
            validator(candidates[0])
        with self.assertRaisesRegex(ValueError, "explicitly flagged section: s2"):
            validator(candidates[1])
        router = ProviderRouter((adapter("mistral", capture_mistral, "script_patch"), adapter("gemini_flash_lite", gemini, "script_patch")))
        with patch("builtins.print"):
            result = router.route(stage="script_patch", prompt=prompt, max_tokens=2200, validator=validator)
        self.assertEqual(calls, ["mistral", "mistral", "gemini_flash_lite"])
        self.assertEqual(script, original)
        self.assertEqual([s["id"] for s in result["sections"]], [s["id"] for s in original["sections"]])
        for index, section in enumerate(original["sections"]):
            if section["id"] in required:
                self.assertNotEqual(result["sections"][index]["narration"], section["narration"])
            else:
                self.assertEqual(result["sections"][index], section)
        if fmt == "short":
            self.assertEqual(result["sections"][-1]["s3_locked_action"], plan["practical_action_ar"])
            pipeline.validate_short_script(result)


def case(fmt, template):
    def test(self):
        self.check_patch(fmt, template)
    return test


for _fmt, _templates in (("short", TEMPLATE_ORDER), ("film", sorted(LONGFORM_NARRATIVE_FORMATS)), ("podcast", ("dialogue_qa",))):
    for _template in _templates:
        setattr(AllTemplatesPatchFeedbackTests, f"test_{_fmt}_{_template}", case(_fmt, _template))


if __name__ == "__main__":
    unittest.main()

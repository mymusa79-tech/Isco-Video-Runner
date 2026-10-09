from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from clean_v2 import pipeline, providers, tone_audit
from clean_v2.contracts import LONGFORM_NARRATIVE_FORMATS
from clean_v2.short_format import TEMPLATE_ORDER, normalize_short_practical_action


def payload(*, flags=(), generic=False):
    return {
        "status": "block" if flags or generic else "pass",
        "preachiness_flags": [], "cultural_dignity_flags": [],
        "naturalness_flags": list(flags), "narrative_format_flags": [],
        "unverified_religious_quote_flags": [], "notes": [],
        "hook_specificity": True, "hook_honesty": True, "hook_curiosity": True,
        "hook_genericness": generic, "hook_body_continuity": True,
        "payoff_resolves_hook": True, "section_dependency": True,
        "topic_fidelity": True, "filler_flags": [], "payoff_earned": True,
        "cold_open_story_violation": False,
    }


def scenario(fmt, template):
    count = {"short": 3, "film": 5, "podcast": 4}[fmt]
    brief = {"format": fmt, "approved_topic": "فخ المقارنة: مسارك الزمني خاص بك وحدك"}
    plan = {
        "title": "فخ المقارنة", "promise": "مقارنة تقدمك بمسارك الشخصي",
        "short_template": template if fmt == "short" else "",
        "narrative_format": template if fmt != "short" else "",
        "practical_action_ar": "قارن تقدمك اليوم بما كنت عليه قبل شهر.",
        "sections": [
            {"id": f"s{i}", "heading": f"فكرة {i}",
             "purpose": f"إضافة المعنى الخاص بالقسم {i}",
             "visual_query_en": f"concrete scene {i}"}
            for i in range(1, count + 1)
        ],
    }
    script = {
        "title": plan["title"],
        "sections": [{"id": f"s{i}", "narration": f"معنى واضح يضيف فكرة جديدة في القسم {i}."}
                     for i in range(1, count + 1)],
    }
    return brief, plan, script


class AllTemplatesContractTests(unittest.TestCase):
    def check_shape(self, fmt, template):
        brief, plan, script = scenario(fmt, template)
        context = pipeline._editorial_shape_contract_context(brief, plan)
        contract = json.loads(context.split("\n")[1])
        self.assertEqual(contract["selected_template"], template)
        self.assertEqual(contract["product_format"], fmt)
        self.assertEqual(contract["closing_section_id"], script["sections"][-1]["id"])
        self.assertEqual(len(contract["ordered_sections"]), len(script["sections"]))
        if fmt == "podcast":
            self.assertIn("listener-proxy", contract["writing_shape"])
        if fmt != "short":
            self.assertEqual(contract["practical_action"], "")
        passing = {**payload(), "provider": "groq", "validation": "valid"}
        with tempfile.TemporaryDirectory() as tmp, patch(
            "clean_v2.tone_audit.audit_tone_and_naturalness_with_mistral",
            return_value=passing,
        ) as audit:
            result = pipeline._run_legacy_tone_naturalness_audit(
                output_dir=Path(tmp), brief=brief, plan=plan, script=script,
            )
        self.assertEqual(result["status"], "pass")
        self.assertEqual(audit.call_args.kwargs["editorial_shape_context"], context)
        for repair in (pipeline._tone_repair_prompt, pipeline._factuality_repair_prompt):
            prompt = repair(brief=brief, plan=plan, script=script, identity={}, cta_plan={},
                            revision_note="s2: repair this sentence's naturalness")
            self.assertIn(context, prompt)
            production_context = prompt.split("PRODUCTION_CONTEXT:\n", 1)[1].split(
                "\n\nREVISION_NOTE:\n", 1
            )[0]
            self.assertEqual(json.loads(production_context)["brief"], brief)


def case(fmt, template):
    def test(self):
        self.check_shape(fmt, template)
    return test


for _fmt, _templates in (("short", TEMPLATE_ORDER),
                         ("film", sorted(LONGFORM_NARRATIVE_FORMATS)),
                         ("podcast", ("dialogue_qa",))):
    for _template in _templates:
        setattr(AllTemplatesContractTests, f"test_{_fmt}_{_template}", case(_fmt, _template))


class Run103RegressionTests(unittest.TestCase):
    def test_run181_real_initial_fragment_stays_blocked_after_grounding(self):
        initial = "عندما تدرك أن المهمة ليست في الإنجاز الكامل."
        first_flag = "s3: fragment starting with 'عندما' lacking main verb."
        initial_result = tone_audit._validate_tone_result(
            tone_audit._ground_syntax_flags(payload(flags=[first_flag]), {"s3": initial})
        )
        self.assertEqual(initial_result["status"], "block")
        narration = "تتحرر من ضغط الإنجاز الكامل حين تدرك أن المهمة ليست في اكتمالها."
        flag = f"s3: '{narration}' — Dangling fragment starting with a conjunction ('حين') attached to a verbal noun ('تدرك') without a finite verb in the sentence."
        result = tone_audit._validate_tone_result(
            tone_audit._ground_syntax_flags(payload(flags=[flag]), {"s3": narration})
        )
        self.assertEqual(result["status"], "pass")
        real_semantic_block = payload(flags=[flag])
        real_semantic_block["narrative_format_flags"] = ["content_depth:s3 practical_action_generic: payoff does not resolve the concrete hook"]
        remaining = tone_audit._validate_tone_result(
            tone_audit._ground_syntax_flags(real_semantic_block, {"s3": narration})
        )
        self.assertEqual(remaining["status"], "block")
        self.assertEqual(remaining["naturalness_flags"], [])

    def test_podcast_depth_repair_carries_actual_draft_with_one_unchanged_floor(self):
        before = {"sections": [{"id": "s1", "narration": "A: سؤال B: " + "معنى " * 258}]}
        self.assertEqual(pipeline._podcast_script_word_count(before), 259)
        after = {"sections": [{"id": "s1", "narration": "A: سؤال B: " + "معنى " * 839}]}
        calls = []
        def route(**kwargs):
            calls.append(kwargs)
            return kwargs["validator"](before if len(calls) == 1 else after)
        result = pipeline._route_script_with_single_podcast_length_repair(
            router=SimpleNamespace(route=route), fmt="podcast", prompt="LOCKED PODCAST PROMPT",
            max_tokens=18000, validator=lambda value: value,
        )
        self.assertEqual(result, after)
        self.assertEqual(len(calls), 2)
        prompt = calls[1]["prompt"]
        current = prompt.split("[CURRENT_PODCAST_SCRIPT]\n", 1)[1].split("\n[/CURRENT_PODCAST_SCRIPT]", 1)[0]
        self.assertEqual(json.loads(current), before)
        self.assertIn("minimum depth deficit is 581 spoken words", prompt)
        self.assertIn("840 words", prompt)
        self.assertIn("expand the B answers", prompt)

    def test_podcast_depth_repair_still_rejects_second_short_sized_draft(self):
        draft = {"sections": [{"id": "s1", "narration": "B: " + "معنى " * 300}]}
        calls = []
        def route(**kwargs):
            calls.append(kwargs)
            return kwargs["validator"](draft)
        with self.assertRaisesRegex(RuntimeError, "words=300 minimum_words=840"):
            pipeline._route_script_with_single_podcast_length_repair(
                router=SimpleNamespace(route=route), fmt="podcast", prompt="PODCAST PROMPT",
                max_tokens=18000, validator=lambda value: value,
            )
        self.assertEqual(len(calls), 2)

    def test_imagined_opening_conjunction_does_not_block_real_nominal_sentence(self):
        narration = "كل خطوة تدوّنها هي دليل على تقدمك، حتى لو لم تره بعد. اختر ثلاثة إنجازات صغيرة هذا الأسبوع."
        flag = f"s3: '{narration}' — dangling fragment starting with a conjunction ('و' implied) without a main verb"
        result = tone_audit._validate_tone_result(
            tone_audit._ground_syntax_flags(payload(flags=[flag]), {"s3": narration})
        )
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["naturalness_flags"], [])
        self.assertIn("Discarded false quoted-prefix claim", result["notes"][0])

    def test_real_fragment_and_unlocated_grammar_flag_still_block(self):
        for narration, flag in (
            ("وتقليل الخيارات إلى ما هو مهم.", "s3: 'وتقليل الخيارات إلى ما هو مهم.' — dangling fragment starting with a conjunction ('و')"),
            ("ومما يجعل الأمر أصعب.", "s3: 'ومما يجعل الأمر أصعب.' — dangling fragment starting with 'مما'"),
            ("والتقليل إلى ما هو مهم.", "s3: 'التقليل إلى ما هو مهم.' — dangling fragment starting with 'و'"),
            ("هذا التوقعات متعبة.", "s3: 'هذا التوقعات' has incorrect demonstrative agreement"),
        ):
            with self.subTest(narration=narration):
                result = tone_audit._validate_tone_result(
                    tone_audit._ground_syntax_flags(payload(flags=[flag]), {"s3": narration})
                )
                self.assertEqual(result["status"], "block")
                self.assertEqual(result["naturalness_flags"], [flag])

    def test_false_prefix_filter_never_clears_real_semantic_rejection(self):
        narration = "كل خطوة واضحة تضيف تقدمًا إلى مسارك."
        flag = f"s3: '{narration}' — fragment starting with 'و'"
        result = tone_audit._validate_tone_result(
            tone_audit._ground_syntax_flags(payload(flags=[flag], generic=True), {"s3": narration})
        )
        self.assertEqual(result["status"], "block")
        self.assertTrue(result["hook_genericness"])
        self.assertTrue(any(f.startswith("hook_quality:") for f in result["narrative_format_flags"]))

    def test_same_call_scope_replaces_the_finite_verb_requirement(self):
        base = tone_audit._LEGACY_FRAGMENT_RULE + "\n" + tone_audit._LEGACY_RELIGIOUS_QUOTE_RULE
        prompt = tone_audit._scope_clean_v2_tone_prompt(base)
        self.assertNotIn("with no independent verb anywhere", prompt)
        self.assertIn("subject and predicate", prompt)
        self.assertIn("EVERY actual", prompt)
        self.assertIn("real grammar, agreement and completeness defects blocking", prompt)

    def test_locked_week_agreement_is_fixed_before_lock_without_paraphrase(self):
        self.assertEqual(
            normalize_short_practical_action("اختر ثلاثة انجازات صغيرة في حياتك هذه الأسبوع."),
            "اختر ثلاثة انجازات صغيرة في حياتك هذا الأسبوع.",
        )
        valid = "قارن هذه الخطوة بخطوتك السابقة."
        self.assertEqual(normalize_short_practical_action(valid), valid)

    def test_spaced_multi_section_semantic_flags_require_both_sections(self):
        _, _, script = scenario("film", "story_analysis")
        for prefix in ("editorial_promise_continuity", "viewer_retention_continuity", "content_depth", "content_dependency"):
            with self.subTest(prefix=prefix):
                revision = f"{prefix}: s2/s3 content_depth: reasoning does not advance"
                self.assertEqual(pipeline._required_semantic_repair_section_ids(script, revision), ("s2", "s3"))
        self.assertEqual(
            pipeline._required_semantic_repair_section_ids(script, "content_dependency: failed section_dependency; s1 is context"),
            (),
        )
        self.assertEqual(
            pipeline._required_semantic_repair_section_ids(script, "viewer_retention_continuity: s1 hook establishes the scene but s2 pivots away"),
            (),
        )
        for revision in ("content_depth:s3 generic action", "content_depth: s3 practical_action_generic: unrelated action", "content_depth: s3"):
            with self.subTest(revision=revision):
                self.assertEqual(pipeline._required_semantic_repair_section_ids(script, revision), ("s3",))

    def test_generic_action_subtype_is_transport_independent_and_narrow(self):
        plan = {"s3_locked_action": "اختر ثلاثة إنجازات صغيرة هذا الأسبوع."}
        for prefix in ("content_depth", "editorial_promise_continuity", "viewer_retention_continuity"):
            with self.subTest(prefix=prefix):
                self.assertTrue(pipeline._short_locked_action_repair_allowed(
                    plan, f"{prefix}: s3 practical_action_generic: unrelated action",
                ))
        self.assertFalse(pipeline._short_locked_action_repair_allowed(plan, "content_depth:s2 generic body"))

    def test_short_writer_does_not_impose_micro_story_on_all_four_templates(self):
        brief, plan, _ = scenario("short", "why_reframe")
        with patch.object(pipeline, "select_short_template", return_value={
            "template": "why_reframe", "writing_directive": "why_reframe", "beat_shape": [],
        }):
            prompt = pipeline._script_prompt(brief, plan, visual_story={})
        self.assertIn("follow the selected template's writing_shape", prompt)
        self.assertNotIn("s3 states the resulting change in that same scene", prompt)

    def test_every_short_template_supplies_repair_shape_guidance(self):
        for template in TEMPLATE_ORDER:
            with self.subTest(template=template), patch.object(pipeline, "select_short_template", return_value={"template": template}):
                note = pipeline._short_template_tone_repair_issue_notes({"format": "short"})
                self.assertIn(f"tone-template:{template}", note)

    def test_original_verdict_is_forwarded_when_preferred_judge_fails(self):
        brief, plan, script = scenario("short", "micro_story")
        production_plan = pipeline._build_production_plan_for_audit(brief=brief, plan=plan, script=script)
        before = copy.deepcopy(script)
        script["sections"][-1]["narration"] = "نتيجة أوضح تتصل بما حدث في المشهد."
        context = pipeline._tone_reaudit_context({"provider": "groq", "naturalness_flags": ["s3: unclear sentence"]}, before, script)
        shape = pipeline._editorial_shape_contract_context(brief, plan)
        with patch.object(providers, "_groq_call", side_effect=RuntimeError("http_429")), patch.object(
            providers, "_gemini_call", side_effect=RuntimeError("http_503")
        ), patch.object(providers, "_openrouter_call", side_effect=RuntimeError("http_429")), patch.object(
            tone_audit, "_mistral_tone_call", return_value=payload()
        ) as last:
            result = tone_audit.audit_tone_and_naturalness_with_mistral(
                "", production_plan, "", preferred_provider="groq",
                editorial_shape_context=shape, repair_verification_context=context,
            )
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["provider"], "mistral")
        last.assert_called_once()
        self.assertIn(context, last.call_args.args[0])
        self.assertIn(shape, last.call_args.args[0])
        verification = json.loads(context.split("\n")[1])
        self.assertEqual(verification["changed_section_ids"], ["s3"])
        self.assertEqual(verification["unchanged_section_ids"], ["s1", "s2"])

    def test_reaudit_keeps_one_repair_and_stops_on_real_remaining_block(self):
        brief, plan, script = scenario("short", "micro_story")
        first = pipeline.CleanV2ToneContentBlock({"provider": "groq", "naturalness_flags": ["s3: unclear sentence"]})
        remaining = pipeline.CleanV2ToneContentBlock({"provider": "mistral", "naturalness_flags": ["s3: real grammar defect"]})
        def repair(**kwargs):
            kwargs["script"]["sections"][-1]["narration"] = "نتيجة أوضح تحتاج تدقيقًا."
            return {"attempts": 1, "narration_changed": True}
        with tempfile.TemporaryDirectory() as tmp, patch.object(pipeline, "_run_text_audits", side_effect=first), patch.object(
            pipeline, "_run_one_bounded_tone_repair", side_effect=repair
        ) as writer, patch.object(pipeline, "_run_legacy_tone_naturalness_audit", side_effect=remaining) as judge, patch.object(
            pipeline, "_run_legacy_factuality_audit", side_effect=AssertionError("must stop at the real block")
        ):
            with self.assertRaises(pipeline.CleanV2ToneContentBlock):
                pipeline._run_text_audit_repair_pass(text_audit=pipeline._run_text_audits, router=SimpleNamespace(),
                    output_dir=Path(tmp), brief=brief, plan=plan, script=script)
            writer.assert_called_once()
            judge.assert_called_once()
            self.assertEqual(judge.call_args.kwargs["preferred_provider"], "groq")
            forwarded = json.loads(judge.call_args.kwargs["repair_verification_context"].split("\n")[1])
            self.assertEqual(forwarded["changed_section_ids"], ["s3"])
            saved = json.loads((Path(tmp) / "tone-repair.json").read_text())
            self.assertEqual(saved["status"], "failed_closed")


if __name__ == "__main__":
    unittest.main()

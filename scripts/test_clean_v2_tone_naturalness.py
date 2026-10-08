from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from clean_v2.identity_sequence import PRAYER_SENTENCE
from clean_v2.pipeline import (
    _closing_payoff_for_tone_audit,
    _factuality_location_issue_notes,
    _factuality_repair_issue_notes,
    _factuality_target_section_ids,
    _first_spoken_sentence,
    _LONGFORM_PROFILES,
    _PODCAST_FIXED_PROFILE,
    _repair_target_section_ids,
    _required_semantic_repair_section_ids,
    _run_legacy_factuality_audit,
    _run_legacy_tone_naturalness_audit,
    _run_one_bounded_tone_repair,
    _run_text_audits,
    _tone_repair_prompt,
    _validate_and_apply_script_patches,
)
from clean_v2.short_format import (
    COLD_OPEN_AS_SCENE,
    HUMAN_VOICE_NO_FILLER,
    TEMPLATE_WRITING_DIRECTIVES,
    ShortFormatError,
)
from clean_v2.tone_audit import (
    TONE_AUDIT_SCHEMA,
    _mistral_tone_call,
    _drop_noop_naturalness_replacements,
    _normalize_editorial_voice_advisory,
    _scope_clean_v2_tone_prompt,
    _scope_religious_quote_prompt,
    _enforce_hook_quality_contract,
)


def _tone_result(*, status: str = "pass", validation: str = "valid") -> dict:
    return {
        "status": status,
        "validation": validation,
        "provider": "gemini" if validation == "valid" else None,
        "attempts": [],
        "preachiness_flags": [],
        "cultural_dignity_flags": [],
        "naturalness_flags": [],
        "narrative_format_flags": [],
        "unverified_religious_quote_flags": [],
        "hook_specificity": True,
        "hook_honesty": True,
        "hook_curiosity": True,
        "hook_genericness": False,
        "hook_body_continuity": True,
        "payoff_resolves_hook": True,
        "notes": [],
    }


class FactualityAvailabilityBoundaryTests(unittest.TestCase):
    def test_low_risk_self_development_text_has_no_local_unavailable_risk(self):
        from clean_v2.pipeline import _factuality_unavailable_local_risks

        script = {
            "title": "خطوة صغيرة",
            "sections": [
                {"id": "s1", "narration": "ابدأ بمهمة واحدة واضحة ثم راقب ما أنجزته بهدوء."}
            ],
        }
        self.assertEqual(_factuality_unavailable_local_risks(script), ())

    def test_medical_or_research_surface_stays_fail_closed_without_provider(self):
        from clean_v2.pipeline import _factuality_unavailable_local_risks

        medical = {
            "sections": [
                {"id": "s1", "narration": "هذا العلاج يخفض ضغط الدم بحسب دراسة بنسبة 20٪."}
            ]
        }
        risks = _factuality_unavailable_local_risks(medical)
        self.assertIn("medical", risks)
        self.assertIn("research_or_statistics", risks)


class CleanV2ToneNaturalnessTests(unittest.TestCase):
    def test_strict_schema_matches_legacy_tone_contract(self):
        self.assertFalse(TONE_AUDIT_SCHEMA["additionalProperties"])
        self.assertEqual(
            set(TONE_AUDIT_SCHEMA["required"]),
            {
                "status",
                "preachiness_flags",
                "cultural_dignity_flags",
                "naturalness_flags",
                "narrative_format_flags",
                "unverified_religious_quote_flags",
                "hook_specificity",
                "hook_honesty",
                "hook_curiosity",
                "hook_genericness",
                "hook_body_continuity",
                "payoff_resolves_hook",
                "section_dependency",
                "topic_fidelity",
                "notes",
            },
        )
        self.assertEqual(
            TONE_AUDIT_SCHEMA["properties"]["status"]["enum"],
            ["pass", "block"],
        )

    def test_mistral_tone_call_uses_strict_schema(self):
        payload = _tone_result()
        captured = {}

        def fake_executor(prompt, **kwargs):
            captured["prompt"] = prompt
            captured.update(kwargs)
            return payload

        with patch("clean_v2.tone_audit.mistral_executor_json", side_effect=fake_executor), patch(
            "clean_v2.tone_audit._validate_tone_result",
            side_effect=lambda value: value,
        ):
            result = _mistral_tone_call("audit prompt")

        self.assertEqual(result, payload)
        self.assertEqual(captured["task_kind"], "text_audit")
        self.assertEqual(captured["temperature"], 0.1)
        name, schema = captured["response_schema"]
        self.assertEqual(name, "clean_v2_tone_naturalness_audit_v2")
        self.assertIs(schema, TONE_AUDIT_SCHEMA)

    def test_hook_quality_prompt_adds_same_call_editorial_dimensions(self):
        base = (
            "5. Unverified religious quotations: flag any religious quotation or attribution presented as authoritative unless the\n"
            "   approved research context directly supports it as verified. Judge this semantically - do not rely only on a fixed\n"
            "   list of marker phrases."
        )
        scoped = _scope_clean_v2_tone_prompt(base)
        for field in (
            "hook_specificity",
            "hook_honesty",
            "hook_curiosity",
            "hook_genericness",
            "hook_body_continuity",
            "payoff_resolves_hook",
        ):
            self.assertIn(field, scoped)
        self.assertIn("calm hooks are fully acceptable", scoped)
        self.assertIn("dozens of unrelated videos", scoped)
        self.assertIn("SAME audit response", scoped)
        self.assertIn("body keeps developing the SAME unresolved tension", scoped)
        self.assertIn("closing payoff directly and satisfactorily", scoped)

    def test_editorial_voice_advisory_prompt_adds_same_call_dimensions(self):
        base = (
            "5. Unverified religious quotations: flag any religious quotation or attribution presented as authoritative unless the\n"
            "   approved research context directly supports it as verified. Judge this semantically - do not rely only on a fixed\n"
            "   list of marker phrases."
        )
        scoped = _scope_clean_v2_tone_prompt(base)
        self.assertIn("EDITORIAL_VOICE_ADVISORY", scoped)
        for field in ("filler_flags", "payoff_earned", "cold_open_story_violation"):
            self.assertIn(field, scoped)
        self.assertIn("this never changes status and never", scoped)
        self.assertIn("blocks production", scoped)
        self.assertIn("textually identical", scoped)
        self.assertIn("'نحن نظن' should be 'نحن نظن'", scoped)

    def test_editorial_voice_advisory_fills_safe_defaults_when_missing(self):
        # Advisory only (session decision: observe before ever gating on this) -
        # a payload from a weak fallback provider that omits all three new
        # fields must still validate cleanly, with safe "no issue found"
        # defaults, never a rejection.
        payload = _tone_result()
        self.assertNotIn("filler_flags", payload)
        self.assertNotIn("payoff_earned", payload)
        self.assertNotIn("cold_open_story_violation", payload)
        validated = _normalize_editorial_voice_advisory(
            _enforce_hook_quality_contract(dict(payload))
        )
        self.assertEqual(validated["filler_flags"], [])
        self.assertIs(validated["payoff_earned"], True)
        self.assertIs(validated["cold_open_story_violation"], False)
        self.assertEqual(validated["status"], "pass")

    def test_editorial_voice_advisory_coerces_malformed_values_without_raising(self):
        result = _normalize_editorial_voice_advisory(
            {
                "filler_flags": "not a list",
                "payoff_earned": "yes",
                "cold_open_story_violation": 1,
            }
        )
        self.assertEqual(result["filler_flags"], [])
        self.assertIs(result["payoff_earned"], True)
        self.assertIs(result["cold_open_story_violation"], False)

    def test_run59_self_identical_naturalness_correction_is_ignored(self):
        payload = _tone_result()
        payload["naturalness_flags"] = [
            "s2: 'نحن نظن' should be 'نحن نظن'"
        ]
        filtered = _drop_noop_naturalness_replacements(payload)
        self.assertEqual(filtered["naturalness_flags"], [])
        self.assertEqual(filtered["status"], "pass")

    def test_real_naturalness_correction_remains_blocking_evidence(self):
        payload = _tone_result()
        payload["naturalness_flags"] = [
            "s3: 'فإنك تحمي عقلك' should be 'بتقليل الخيارات، أنت تحمي عقلك'"
        ]
        filtered = _drop_noop_naturalness_replacements(payload)
        self.assertEqual(
            filtered["naturalness_flags"],
            payload["naturalness_flags"],
        )

    def test_noop_filter_does_not_touch_non_replacement_grammar_flags(self):
        payload = _tone_result()
        payload["naturalness_flags"] = [
            "s3: dependent fragment begins with فإنك without a preceding condition"
        ]
        filtered = _drop_noop_naturalness_replacements(payload)
        self.assertEqual(
            filtered["naturalness_flags"],
            payload["naturalness_flags"],
        )

    def test_editorial_voice_advisory_never_forces_a_block(self):
        # Even a maximally "bad" advisory verdict (filler everywhere, payoff
        # not earned, cold-open violated) must leave an otherwise-passing
        # audit at status=pass - this is observation only, not a gate.
        payload = _tone_result()
        payload["filler_flags"] = ["كل جملة هنا حشو."]
        payload["payoff_earned"] = False
        payload["cold_open_story_violation"] = True
        validated = _normalize_editorial_voice_advisory(
            _enforce_hook_quality_contract(dict(payload))
        )
        self.assertEqual(validated["status"], "pass")
        self.assertEqual(validated["filler_flags"], ["كل جملة هنا حشو."])
        self.assertIs(validated["payoff_earned"], False)
        self.assertIs(validated["cold_open_story_violation"], True)

    def test_generic_hook_example_is_rejected(self):
        payload = _tone_result()
        payload.update(
            {
                "hook_specificity": False,
                "hook_honesty": False,
                "hook_curiosity": False,
                "hook_genericness": True,
                "notes": ["hook=غيّر حياتك اليوم."],
            }
        )
        result = _enforce_hook_quality_contract(payload)
        self.assertEqual(result["status"], "block")
        hook_flags = [
            item
            for item in result["narrative_format_flags"]
            if item.startswith("hook_quality:")
        ]
        self.assertEqual(len(hook_flags), 1)
        self.assertIn("hook_specificity", hook_flags[0])
        self.assertIn("hook_genericness", hook_flags[0])

    def test_hook_that_drops_after_opening_or_lacks_payoff_is_rejected(self):
        payload = _tone_result()
        payload["hook_body_continuity"] = False
        payload["payoff_resolves_hook"] = False
        result = _enforce_hook_quality_contract(payload)
        self.assertEqual(result["status"], "block")
        hook_flag = next(
            item
            for item in result["narrative_format_flags"]
            if item.startswith("hook_quality:")
        )
        self.assertIn("hook_body_continuity", hook_flag)
        self.assertIn("payoff_resolves_hook", hook_flag)

    def test_specific_quiet_hook_example_is_accepted(self):
        payload = _tone_result()
        payload["notes"] = ["hook=لماذا تنتهي خطتك كل يوم عند أول مقاطعة؟"]
        result = _enforce_hook_quality_contract(payload)
        self.assertEqual(result["status"], "pass")
        self.assertFalse(
            any(
                item.startswith("hook_quality:")
                for item in result["narrative_format_flags"]
            )
        )

    def test_hook_quality_repair_targets_and_replaces_only_first_hook(self):
        plan = {
            "title": "اختبار",
            "sections": [
                {"id": f"s{index}", "heading": "h", "purpose": "p", "visual_query_en": "desk"}
                for index in range(1, 6)
            ],
        }
        script = {
            "title": "اختبار",
            "sections": [
                {"id": "s1", "narration": "غيّر حياتك اليوم. هذه بداية شرح مرتبطة بالموضوع."},
                {"id": "s2", "narration": "هذه فقرة ثانية تحتوي شرحًا كافيًا للاختبار."},
                {"id": "s3", "narration": "هذه فقرة ثالثة تحتوي شرحًا كافيًا للاختبار."},
                {"id": "s4", "narration": "هذه فقرة رابعة تحتوي شرحًا كافيًا للاختبار."},
                {"id": "s5", "narration": "هذه فقرة أخيرة تحتوي خاتمة كافية للاختبار."},
            ],
        }
        revision = "- [tone] hook_quality: failed hook_specificity, hook_genericness"
        # Run #68: scoping a hook rewrite to s1 alone left s2-s5 anchored to the
        # OLD hook's specific tension, and the re-audit then failed
        # hook_specificity/section_dependency/payoff_resolves_hook on the body
        # even though the hook fix itself was correct. The allowed scope now
        # includes every section so the repair CAN realign them if needed -
        # this test's own patch set below still only touches s1, proving the
        # widened scope does not force unrelated sections to be touched.
        self.assertEqual(
            _repair_target_section_ids(script, revision, {}),
            ("s1", "s2", "s3", "s4", "s5"),
        )
        repaired = _validate_and_apply_script_patches(
            {
                "patches": [
                    {
                        "section_id": "s1",
                        "find": "غيّر حياتك اليوم.",
                        "replace": "لماذا تنتهي خطتك كل يوم عند أول مقاطعة؟",
                    }
                ]
            },
            plan=plan,
            original_script=script,
            identity={},
            cta_plan={},
            revision_note=revision,
        )
        self.assertTrue(
            repaired["sections"][0]["narration"].startswith(
                "لماذا تنتهي خطتك كل يوم عند أول مقاطعة؟"
            )
        )
        self.assertIn("هذه بداية شرح مرتبطة بالموضوع.", repaired["sections"][0]["narration"])

    def test_run29_patch_replace_text_normalizes_tanween_fath_off_the_alef(self):
        # Podcast Run #29: a bounded repair fixed the real flagged defects
        # (hook_curiosity, payoff_resolves_hook) perfectly, but the model's
        # rewrite naturally used "فعلاً" (tanween fath on the alif) - and the
        # re-audit's naturalness gate flagged it, since the classically-
        # preferred placement is on the letter before the alif ("فعلًا").
        # With no repair attempt left, the whole production failed on a
        # cosmetic two-character reorder despite the real fix succeeding.
        # Patch replace text is now normalized to that preferred form
        # automatically, so this class of near-miss never reaches the audit.
        plan = {
            "title": "اختبار",
            "sections": [
                {"id": "s1", "heading": "h1", "purpose": "p1", "visual_query_en": "desk"},
                {"id": "s2", "heading": "h2", "purpose": "p2", "visual_query_en": "window"},
            ],
        }
        script = {
            "title": "اختبار",
            "sections": [
                {"id": "s1", "narration": "هذه فقرة تحتوي شرحًا كافيًا للاختبار."},
                {"id": "s2", "narration": "الخاتمة القديمة هنا."},
            ],
        }
        revision = "- [tone] content_depth:s2"
        repaired = _validate_and_apply_script_patches(
            {
                "patches": [
                    {
                        "section_id": "s2",
                        "find": "الخاتمة القديمة هنا.",
                        "replace": "هل تجاهلت الأمر فعلاً حقاً أم غالباً قريباً ستنتبه؟",
                    }
                ]
            },
            plan=plan,
            original_script=script,
            identity={},
            cta_plan={},
            revision_note=revision,
            allowed_section_ids=("s2",),
        )
        narration = repaired["sections"][1]["narration"]
        self.assertIn("فعلًا", narration)
        self.assertIn("حقًا", narration)
        self.assertIn("غالبًا", narration)
        self.assertIn("قريبًا", narration)
        self.assertNotIn("فعلاً", narration)
        self.assertNotIn("حقاً", narration)
        self.assertNotIn("غالباً", narration)
        self.assertNotIn("قريباً", narration)

    def _short_plan_and_script(self):
        plan = {
            "title": "اختبار",
            "sections": [
                {"id": "s1", "heading": "h1", "purpose": "p1", "visual_query_en": "desk"},
                {"id": "s2", "heading": "h2", "purpose": "p2", "visual_query_en": "window"},
                {"id": "s3", "heading": "h3", "purpose": "p3", "visual_query_en": "sunrise"},
            ],
        }
        script = {
            "title": "اختبار",
            "sections": [
                {
                    "id": "s1",
                    "narration": "لماذا تشعر بالإرهاق كل مساء دون أن تعرف السبب الحقيقي؟",
                },
                {"id": "s2", "narration": "هذه فقرة ثانية تشرح الفكرة بعمق كافٍ للاختبار."},
                {
                    "id": "s3",
                    "narration": (
                        "ابدأ بخطوة واحدة صغيرة الآن. "
                        "ستشعر بارتياح واضح مع نهاية اليوم."
                    ),
                },
            ],
        }
        return plan, script

    def test_run37_repair_deleting_s3_practical_action_is_rejected_for_short(self):
        # Telegram Run #37 (today): the initial audit flagged s3 for
        # grammatical errors/unnatural phrasing. The bounded repair fixed
        # that legitimately via Groq - the re-audit came back completely
        # clean - but the rewrite also incidentally deleted s3's required
        # practical-action sentence. Nothing in the tone-repair path knew
        # about that independent Short contract (short_format.validate_
        # short_script), so the break escaped as a "valid" repair and only
        # surfaced ~100 lines later, in an unrelated pipeline stage, as an
        # uncaught ShortFormatError with no link back to the repair that
        # caused it. is_short_format=True must catch this right here.
        plan, script = self._short_plan_and_script()
        original_s3 = script["sections"][2]["narration"]
        revision = "- [tone] s3 narration has grammatical errors and unnatural phrasing."
        with self.assertRaisesRegex(
            ShortFormatError, "short_s3_requires_exactly_one_practical_action"
        ):
            _validate_and_apply_script_patches(
                {
                    "patches": [
                        {
                            "section_id": "s3",
                            "find": original_s3,
                            "replace": "شعرت أن اليوم انتهى دون أن تحقق شيئًا يُذكر.",
                        }
                    ]
                },
                plan=plan,
                original_script=script,
                identity={},
                cta_plan={},
                revision_note=revision,
                allowed_section_ids=("s3",),
                is_short_format=True,
            )

    def test_run37_same_patch_is_not_checked_for_non_short_formats(self):
        # The new short-only contract check must not reach into Podcast/Film
        # repairs, which have no s3-practical-action concept at all and are
        # validated by their own independent contracts.
        plan, script = self._short_plan_and_script()
        original_s3 = script["sections"][2]["narration"]
        revision = "- [tone] s3 narration has grammatical errors and unnatural phrasing."
        repaired = _validate_and_apply_script_patches(
            {
                "patches": [
                    {
                        "section_id": "s3",
                        "find": original_s3,
                        "replace": "شعرت أن اليوم انتهى دون أن تحقق شيئًا يُذكر.",
                    }
                ]
            },
            plan=plan,
            original_script=script,
            identity={},
            cta_plan={},
            revision_note=revision,
            allowed_section_ids=("s3",),
        )
        self.assertEqual(
            repaired["sections"][2]["narration"],
            "شعرت أن اليوم انتهى دون أن تحقق شيئًا يُذكر.",
        )

    def test_run63_structured_short_patch_changes_payoff_not_locked_action(self):
        action = "اكتب هدفًا شخصيًا واحدًا اليوم."
        plan = {
            "title": "اختبار",
            "practical_action_ar": action,
            "s3_locked_action": action,
            "sections": [
                {"id": "s1", "heading": "h1", "purpose": "p1", "visual_query_en": "desk"},
                {"id": "s2", "heading": "h2", "purpose": "p2", "visual_query_en": "window"},
                {"id": "s3", "heading": "h3", "purpose": "p3", "visual_query_en": "sunrise"},
            ],
        }
        payoff = "مسارك الزمني خاص بك، وقيمتك لا تُقاس بسرعة شخص آخر."
        script = {
            "title": "اختبار",
            "sections": [
                {
                    "id": "s1",
                    "narration": "هل تقارن يومك بمسار شخص آخر ثم تعتبر نفسك متأخرًا رغم اختلاف الطريق؟",
                },
                {
                    "id": "s2",
                    "narration": "حين يتغير معيار القياس كل مرة، يبدو تقدمك أصغر حتى لو كان حقيقيًا.",
                },
                {
                    "id": "s3",
                    "narration": f"{payoff} {action}",
                    "s3_payoff": payoff,
                    "s3_locked_action": action,
                },
            ],
        }

        repaired = _validate_and_apply_script_patches(
            {
                "patches": [
                    {
                        "section_id": "s3",
                        "find": "وقيمتك لا تُقاس بسرعة شخص آخر",
                        "replace": "وقيمتك تُقاس بتقدمك أنت لا بسرعة شخص آخر",
                    }
                ]
            },
            plan=plan,
            original_script=script,
            identity={},
            cta_plan={},
            revision_note="- [tone] content_depth:s3",
            allowed_section_ids=("s3",),
            is_short_format=True,
        )
        closing = repaired["sections"][2]
        self.assertEqual(closing["s3_locked_action"], action)
        self.assertTrue(closing["narration"].endswith(action))
        self.assertIn("بتقدمك أنت", closing["s3_payoff"])

    def test_run63_patch_touching_locked_action_is_terminal_local_rejection(self):
        action = "اكتب هدفًا شخصيًا واحدًا اليوم."
        plan = {
            "title": "اختبار",
            "practical_action_ar": action,
            "s3_locked_action": action,
            "sections": [
                {"id": "s1", "heading": "h1", "purpose": "p1", "visual_query_en": "desk"},
                {"id": "s2", "heading": "h2", "purpose": "p2", "visual_query_en": "window"},
                {"id": "s3", "heading": "h3", "purpose": "p3", "visual_query_en": "sunrise"},
            ],
        }
        payoff = "مسارك الزمني خاص بك، وقيمتك لا تُقاس بسرعة شخص آخر."
        script = {
            "title": "اختبار",
            "sections": [
                {
                    "id": "s1",
                    "narration": "هل تقارن يومك بمسار شخص آخر ثم تعتبر نفسك متأخرًا رغم اختلاف الطريق؟",
                },
                {
                    "id": "s2",
                    "narration": "حين يتغير معيار القياس كل مرة، يبدو تقدمك أصغر حتى لو كان حقيقيًا.",
                },
                {
                    "id": "s3",
                    "narration": f"{payoff} {action}",
                    "s3_payoff": payoff,
                    "s3_locked_action": action,
                },
            ],
        }

        with self.assertRaises(ValueError) as caught:
            _validate_and_apply_script_patches(
                {
                    "patches": [
                        {
                            "section_id": "s3",
                            "find": action,
                            "replace": "اختر معيارًا جديدًا للمقارنة.",
                        }
                    ]
                },
                plan=plan,
                original_script=script,
                identity={},
                cta_plan={},
                revision_note="- [tone] content_depth:s3",
                allowed_section_ids=("s3",),
                is_short_format=True,
            )
        self.assertTrue(
            getattr(caught.exception, "terminal_provider_fallback", False)
        )

    def test_run102_authorized_wide_s3_patch_is_non_terminal_and_later_patches_survive(self):
        action = "حدد خيارًا واحدًا الآن."
        plan = {
            "title": "اختبار",
            "practical_action_ar": action,
            "s3_locked_action": action,
            "sections": [
                {"id": "s1", "heading": "h1", "purpose": "p1", "visual_query_en": "store"},
                {"id": "s2", "heading": "h2", "purpose": "p2", "visual_query_en": "tabs"},
                {"id": "s3", "heading": "h3", "purpose": "p3", "visual_query_en": "choice"},
            ],
        }
        payoff = "ستجد أن الحسم أصبح أسهل وأكثر راحة."
        script = {
            "title": "اختبار",
            "sections": [
                {"id": "s1", "narration": "لماذا تتوقف عن الاختيار كلما زادت البدائل أمامك؟"},
                {"id": "s2", "narration": "المقارنة المستمرة تجعل كل بديل يبدو كخسارة محتملة لبديل آخر."},
                {
                    "id": "s3",
                    "narration": f"{payoff} {action}",
                    "s3_payoff": payoff,
                    "s3_locked_action": action,
                },
            ],
        }
        revision = (
            "- [tone] content_dependency:s3 الخاتمة منفصلة عن مبدأ الاكتفاء بخيار مناسب.\n"
            "- [tone] content_depth:s3 practical_action_generic: الإجراء عام ولا يكبح البحث عن الخيار المثالي."
        )
        repaired = _validate_and_apply_script_patches(
            {
                "patches": [
                    {
                        # Malformed provider patch spanning payoff + locked action:
                        # authorized action repair must reject it locally, not abort
                        # the whole provider candidate as a terminal route failure.
                        "section_id": "s3",
                        "find": f"{payoff} {action}",
                        "replace": "حين تقبل خيارًا مناسبًا بدل مطاردة المثالي، يصبح القرار أخف. اختر البديل الذي يفي بحاجتك الآن.",
                    },
                    {
                        "section_id": "s3",
                        "find": payoff,
                        "replace": "حين تتوقف عن مطاردة الخيار المثالي، يصبح الحسم أخف وأكثر وضوحًا.",
                    },
                    {
                        "section_id": "s3",
                        "find": action,
                        "replace": "اختر بديلًا واحدًا يفي بحاجتك الآن.",
                    },
                ]
            },
            plan=plan,
            original_script=script,
            identity={},
            cta_plan={},
            revision_note=revision,
            allowed_section_ids=("s3",),
            is_short_format=True,
            allow_short_locked_action_repair=True,
            required_changed_section_ids=("s3",),
        )
        closing = repaired["sections"][2]
        self.assertIn("مطاردة الخيار المثالي", closing["s3_payoff"])
        self.assertEqual(
            closing["s3_locked_action"],
            "اختر بديلًا واحدًا يفي بحاجتك الآن.",
        )
        self.assertTrue(closing["narration"].endswith(closing["s3_locked_action"]))

    def test_run37_harmless_short_s3_payoff_patch_still_passes(self):
        # No false positive: a repair that leaves the action sentence intact
        # and only rewords the payoff clause must still be accepted.
        plan, script = self._short_plan_and_script()
        revision = "- [tone] s3 narration has grammatical errors and unnatural phrasing."
        repaired = _validate_and_apply_script_patches(
            {
                "patches": [
                    {
                        "section_id": "s3",
                        "find": "ستشعر بارتياح واضح مع نهاية اليوم.",
                        "replace": "ستشعر بارتياح أعمق بحلول المساء.",
                    }
                ]
            },
            plan=plan,
            original_script=script,
            identity={},
            cta_plan={},
            revision_note=revision,
            allowed_section_ids=("s3",),
            is_short_format=True,
        )
        narration = repaired["sections"][2]["narration"]
        self.assertTrue(narration.startswith("ابدأ بخطوة واحدة صغيرة الآن."))
        self.assertIn("ستشعر بارتياح أعمق بحلول المساء.", narration)

    def test_run135_multi_sentence_hook_rewrite_is_trimmed_to_first_sentence(self):
        # Run #135: a plausible, genuinely-permitted hook rewrite that accidentally
        # carried a second sentence used to be rejected outright with no second try.
        plan = {
            "title": "اختبار",
            "sections": [
                {"id": f"s{index}", "heading": "h", "purpose": "p", "visual_query_en": "desk"}
                for index in range(1, 3)
            ],
        }
        script = {
            "title": "اختبار",
            "sections": [
                {"id": "s1", "narration": "غيّر حياتك اليوم. هذه بداية شرح مرتبطة بالموضوع."},
                {"id": "s2", "narration": "هذه فقرة ثانية تحتوي شرحًا كافيًا للاختبار."},
            ],
        }
        revision = "- [tone] hook_quality: failed hook_specificity, hook_genericness"
        repaired = _validate_and_apply_script_patches(
            {
                "patches": [
                    {
                        "section_id": "s1",
                        "find": "غيّر حياتك اليوم.",
                        "replace": "لماذا تنتهي خطتك كل يوم عند أول مقاطعة؟ وهذه جملة ثانية زائدة.",
                    }
                ]
            },
            plan=plan,
            original_script=script,
            identity={},
            cta_plan={},
            revision_note=revision,
        )
        narration = repaired["sections"][0]["narration"]
        self.assertTrue(narration.startswith("لماذا تنتهي خطتك كل يوم عند أول مقاطعة؟"))
        self.assertNotIn("جملة ثانية زائدة", narration)
        self.assertIn("هذه بداية شرح مرتبطة بالموضوع.", narration)

    def test_run135_imprecise_find_still_uses_verified_original_hook(self):
        # Run #135: the model must echo the exact original hook back in `find` for a
        # full-hook rewrite to be accepted. A model quoting it slightly imprecisely
        # (here, missing the trailing period) used to be rejected outright, even
        # though the section-scope substring gate already confirmed intent. The
        # verified original_hook is now always the actual find-target.
        plan = {
            "title": "اختبار",
            "sections": [
                {"id": f"s{index}", "heading": "h", "purpose": "p", "visual_query_en": "desk"}
                for index in range(1, 3)
            ],
        }
        script = {
            "title": "اختبار",
            "sections": [
                {"id": "s1", "narration": "غيّر حياتك اليوم. هذه بداية شرح مرتبطة بالموضوع."},
                {"id": "s2", "narration": "هذه فقرة ثانية تحتوي شرحًا كافيًا للاختبار."},
            ],
        }
        revision = "- [tone] hook_quality: failed hook_specificity, hook_genericness"
        repaired = _validate_and_apply_script_patches(
            {
                "patches": [
                    {
                        "section_id": "s1",
                        "find": "غيّر حياتك اليوم",  # missing the trailing period
                        "replace": "لماذا تنتهي خطتك كل يوم عند أول مقاطعة؟",
                    }
                ]
            },
            plan=plan,
            original_script=script,
            identity={},
            cta_plan={},
            revision_note=revision,
        )
        narration = repaired["sections"][0]["narration"]
        self.assertTrue(narration.startswith("لماذا تنتهي خطتك كل يوم عند أول مقاطعة؟"))
        self.assertIn("هذه بداية شرح مرتبطة بالموضوع.", narration)

    def test_run36_wide_hook_find_is_canonicalized_without_touching_prayer_or_body(self):
        # Podcast Run #36: Mistral copied the complete hook together with adjacent
        # host-owned text. The intended hook repair was valid, but the old one-way
        # substring check never entered the safe hook canonicalizer and the whole
        # provider route exhausted on "changed the locked hook".
        plan = {
            "title": "اختبار",
            "sections": [
                {"id": "s1", "heading": "h1", "purpose": "p1", "visual_query_en": "desk"},
                {"id": "s2", "heading": "h2", "purpose": "p2", "visual_query_en": "window"},
            ],
        }
        original_hook = "A: كيف نعرف النصيحة الصحيحة ثم نكرر الخطأ نفسه؟"
        first_answer = "B: لأن المعرفة وحدها لا تزيل احتكاك البداية."
        script = {
            "title": "اختبار",
            "sections": [
                {
                    "id": "s1",
                    "narration": f"{original_hook} {PRAYER_SENTENCE} {first_answer}",
                },
                {"id": "s2", "narration": "هذه فقرة ثانية تحتوي شرحًا كافيًا للاختبار."},
            ],
        }
        revision = "- [tone] hook_quality: failed hook_genericness"
        wide_find = f"{original_hook} {PRAYER_SENTENCE}"
        repaired = _validate_and_apply_script_patches(
            {
                "patches": [
                    {
                        "section_id": "s1",
                        "find": wide_find,
                        # Omitting A: is tolerated locally, then the verified
                        # original speaker ownership is restored deterministically.
                        "replace": "لماذا لا تغيّر النصيحة الصحيحة سلوكنا تلقائيًا؟",
                    }
                ]
            },
            plan=plan,
            original_script=script,
            identity={},
            cta_plan={},
            revision_note=revision,
        )

        narration = repaired["sections"][0]["narration"]
        self.assertTrue(
            narration.startswith(
                "A: لماذا لا تغيّر النصيحة الصحيحة سلوكنا تلقائيًا؟"
            )
        )
        self.assertEqual(narration.count(PRAYER_SENTENCE), 1)
        self.assertIn(first_answer, narration)
        self.assertNotIn(original_hook, narration)

        # The same wide span must remain locked when only the payoff/body was
        # flagged; broad-span tolerance is not permission to rewrite a good hook.
        with self.assertRaisesRegex(ValueError, "script patch changed (?:the )?locked hook"):
            _validate_and_apply_script_patches(
                {
                    "patches": [
                        {
                            "section_id": "s1",
                            "find": wide_find,
                            "replace": "لماذا لا تغيّر النصيحة الصحيحة سلوكنا تلقائيًا؟",
                        }
                    ]
                },
                plan=plan,
                original_script=script,
                identity={},
                cta_plan={},
                revision_note="- [tone] hook_quality: payoff_resolves_hook=false",
                allowed_section_ids=("s1",),
            )

    def test_run135_overlong_hook_rewrite_is_trimmed_at_word_boundary(self):
        # Run #135: a rewrite a few characters over the 220-char cap used to be
        # rejected outright rather than trimmed, within the general per-patch
        # expansion budget (len(find) + 180) that still governs how much longer a
        # replacement may be than what it replaces.
        plan = {
            "title": "اختبار",
            "sections": [
                {"id": f"s{index}", "heading": "h", "purpose": "p", "visual_query_en": "desk"}
                for index in range(1, 3)
            ],
        }
        original_hook = (
            "لماذا نكرر نفس الخطأ في كل مرة نحاول فيها الالتزام بعادة جديدة "
            "رغم معرفتنا بنتيجته سلفًا؟"
        )
        script = {
            "title": "اختبار",
            "sections": [
                {
                    "id": "s1",
                    "narration": f"{original_hook} هذه بداية شرح مرتبطة بالموضوع.",
                },
                {"id": "s2", "narration": "هذه فقرة ثانية تحتوي شرحًا كافيًا للاختبار."},
            ],
        }
        revision = "- [tone] hook_quality: failed hook_specificity, hook_genericness"
        long_replace = (
            "لماذا نستمر في تكرار نفس الخطأ تمامًا في كل مرة نحاول فيها الالتزام بعادة "
            "جديدة رغم أننا نعرف نتيجته سلفًا تمام المعرفة ونشعر بالإحباط ذاته في كل "
            "محاولة جديدة نبدأها بحماس واضح ثم نتوقف عنها بسرعة أكبر من المرة السابقة "
            "تمامًا كما توقعنا؟"
        )
        self.assertGreater(len(long_replace), 220)
        self.assertLessEqual(len(long_replace), len(original_hook) + 180)

        repaired = _validate_and_apply_script_patches(
            {
                "patches": [
                    {
                        "section_id": "s1",
                        "find": original_hook,
                        "replace": long_replace,
                    }
                ]
            },
            plan=plan,
            original_script=script,
            identity={},
            cta_plan={},
            revision_note=revision,
        )
        result_hook = _first_spoken_sentence(repaired)
        self.assertLessEqual(len(result_hook), 220)
        self.assertTrue(result_hook.startswith("لماذا نستمر في تكرار نفس الخطأ"))
        self.assertIn("هذه بداية شرح مرتبطة بالموضوع.", repaired["sections"][0]["narration"])

    def test_hook_quality_repair_prompt_opens_only_flagged_hook(self):
        plan = {
            "title": "اختبار",
            "sections": [{"id": "s1"}],
        }
        script = {
            "title": "اختبار",
            "sections": [{"id": "s1", "narration": "غيّر حياتك اليوم. هذه بداية شرح."}],
        }
        prompt = _tone_repair_prompt(
            brief={"format": "film", "research_pack": []},
            plan=plan,
            script=script,
            identity={},
            cta_plan={},
            revision_note="- [tone] hook_quality: failed hook_specificity",
        )
        self.assertIn("Replace the complete first spoken hook sentence exactly once", prompt)
        self.assertIn("Calm is acceptable; forced shock/clickbait is not", prompt)

    def test_run27_payoff_mismatch_alone_targets_closing_section_not_hook(self):
        # Run #27: the hook passed every one of its own checks (specificity/honesty/
        # curiosity/genericness all clean) - only payoff_resolves_hook was false. The
        # repair used to treat this as "the hook is the defect" anyway, rewrote an
        # already-good hook, and the rewrite came back flagged hook_genericness=true
        # on re-audit. The fix owns the mismatch from the closing/payoff side instead.
        plan = {
            "title": "اختبار",
            "sections": [
                {"id": "s1", "heading": "h1", "purpose": "p1", "visual_query_en": "desk"},
                {"id": "s2", "heading": "h2", "purpose": "p2", "visual_query_en": "window"},
            ],
        }
        script = {
            "title": "اختبار",
            "sections": [
                {"id": "s1", "narration": "لماذا نعود إلى عادة نعرف أنها تؤذينا؟ هذا شرح كافٍ."},
                {"id": "s2", "narration": "هذه خاتمة لا ترتبط مباشرة بسؤال البداية."},
            ],
        }
        revision = "- [tone] hook_quality: payoff_resolves_hook=false"

        self.assertEqual(
            _repair_target_section_ids(script, revision, {}),
            ("s2",),
        )

        prompt = _tone_repair_prompt(
            brief={"format": "podcast", "research_pack": []},
            plan=plan,
            script=script,
            identity={},
            cta_plan={},
            revision_note=revision,
        )
        self.assertIn(
            "Preserve this first spoken hook sentence exactly", prompt
        )
        self.assertNotIn("Replace the complete first spoken hook sentence", prompt)
        self.assertIn("fix the mismatch by adjusting the closing/body content", prompt)

        # Isolate the hard hook-lock enforcement itself (independent of the section-
        # scope check above) by explicitly allowing s1 as a target: even then, a full
        # hook rewrite must still be rejected for a payoff-only mismatch.
        with self.assertRaisesRegex(ValueError, "script patch changed the locked hook"):
            _validate_and_apply_script_patches(
                {
                    "patches": [
                        {
                            "section_id": "s1",
                            "find": "لماذا نعود إلى عادة نعرف أنها تؤذينا؟",
                            "replace": "هل تكرر نفس الخطأ رغم معرفتك بنتيجته؟",
                        }
                    ]
                },
                plan=plan,
                original_script=script,
                identity={},
                cta_plan={},
                revision_note=revision,
                allowed_section_ids=("s1",),
            )

    def test_run27_hook_body_continuity_alone_also_preserves_the_hook(self):
        plan = {
            "title": "اختبار",
            "sections": [{"id": "s1", "heading": "h", "purpose": "p", "visual_query_en": "desk"}],
        }
        script = {
            "title": "اختبار",
            "sections": [{"id": "s1", "narration": "لماذا نعود إلى عادة نعرف أنها تؤذينا؟ شرح."}],
        }
        revision = "- [tone] hook_quality: hook_body_continuity=false"

        prompt = _tone_repair_prompt(
            brief={"format": "podcast", "research_pack": []},
            plan=plan,
            script=script,
            identity={},
            cta_plan={},
            revision_note=revision,
        )
        self.assertIn(
            "Preserve this first spoken hook sentence exactly", prompt
        )
        self.assertNotIn("Replace the complete first spoken hook sentence", prompt)

    def test_genuine_hook_defect_combined_with_payoff_mismatch_targets_both(self):
        plan = {
            "title": "اختبار",
            "sections": [
                {"id": "s1", "heading": "h1", "purpose": "p1", "visual_query_en": "desk"},
                {"id": "s2", "heading": "h2", "purpose": "p2", "visual_query_en": "window"},
            ],
        }
        script = {
            "title": "اختبار",
            "sections": [
                {"id": "s1", "narration": "غيّر حياتك اليوم. هذا شرح كافٍ للاختبار."},
                {"id": "s2", "narration": "خاتمة لا ترتبط بالسؤال."},
            ],
        }
        revision = (
            "- [tone] hook_quality: failed hook_genericness, payoff_resolves_hook"
        )

        self.assertEqual(
            _repair_target_section_ids(script, revision, {}),
            ("s1", "s2"),
        )

        prompt = _tone_repair_prompt(
            brief={"format": "podcast", "research_pack": []},
            plan=plan,
            script=script,
            identity={},
            cta_plan={},
            revision_note=revision,
        )
        self.assertIn("Replace the complete first spoken hook sentence exactly once", prompt)

    def test_first_spoken_sentence_is_runtime_hook(self):
        script = {
            "sections": [
                {
                    "id": "s1",
                    "narration": "لماذا نخطط كثيرًا ولا نبدأ؟ الجواب ليس نقص الوقت.",
                }
            ]
        }
        self.assertEqual(
            _first_spoken_sentence(script),
            "لماذا نخطط كثيرًا ولا نبدأ؟",
        )

    def test_run254_tone_bridge_uses_actual_closing_payoff_and_identity(self):
        script = {
            "sections": [
                {"id": "s1", "narration": "هوك فعلي واضح. ثم بداية الشرح."},
                {
                    "id": "s5",
                    "narration": (
                        "اختر إشارة واحدة واضحة وجرّبها غدًا. "
                        "راقب هل قرّبت خطتك من الواقع بدل الأمل. "
                        "هذه هي الخلاصة التي نريد أن تبقى. "
                        "إلى لقاء جديد مع اليقظة، وحفظكم الله."
                    ),
                },
            ]
        }
        identity = {
            "opener": "افتتاح الهوية",
            "closer": "إلى لقاء جديد مع اليقظة، وحفظكم الله.",
            "transitions": ["أولاً", "ثم", "أخيرًا"],
        }
        payoff = _closing_payoff_for_tone_audit(script, identity=identity)
        self.assertNotIn(identity["closer"], payoff)
        self.assertIn("قرّبت خطتك من الواقع بدل الأمل", payoff)
        self.assertIn("هذه هي الخلاصة", payoff)

        captured = {}
        dummy_plan = SimpleNamespace(
            hook="",
            closing_payoff="وعد التخطيط الذي لا يجب تقييمه كخاتمة",
            identity_opener="",
            identity_closer="",
            identity_transitions=[],
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "narrative-identity.json").write_text(
                json.dumps(identity, ensure_ascii=False),
                encoding="utf-8",
            )

            def audit(_api_key, production_plan, _model, **kwargs):
                captured["plan"] = production_plan
                captured["research_boundaries"] = kwargs.get("research_boundaries", "")
                return _tone_result()

            with patch(
                "clean_v2.pipeline._build_production_plan_for_audit",
                return_value=dummy_plan,
            ), patch(
                "clean_v2.tone_audit.audit_tone_and_naturalness_with_mistral",
                side_effect=audit,
            ):
                _run_legacy_tone_naturalness_audit(
                    output_dir=root,
                    brief={"format": "film"},
                    plan={"promise": "وعد التخطيط"},
                    script=script,
                )

        plan = captured["plan"]
        self.assertEqual(plan.hook, "هوك فعلي واضح.")
        self.assertEqual(plan.closing_payoff, payoff)
        self.assertNotEqual(plan.closing_payoff, "وعد التخطيط")
        self.assertEqual(plan.identity_opener, identity["opener"])
        self.assertEqual(plan.identity_closer, identity["closer"])
        self.assertEqual(plan.identity_transitions, identity["transitions"])

    def test_run82_semantic_repair_requires_every_explicitly_flagged_section(self):
        script = {
            "title": "وهم الكفاءة",
            "sections": [
                {"id": "s1", "narration": "A: هل نتحكم في التقنية أم نتبع ما تعرضه علينا؟"},
                {"id": "s2", "narration": "B: تتكرر أمامك اقتراحات قريبة مما شاهدته سابقًا."},
                {"id": "s3", "narration": "B: اكتب فكرتك قبل أن تفتح البحث."},
            ],
        }
        revision = "\n".join(
            (
                "- [tone] editorial_promise_continuity:s2 does not directly earn the hook tension.",
                "- [tone] viewer_retention_continuity:s3 does not resolve the specific hook.",
                "- [tone] content_depth:s3 generic action.",
                "- [tone] hook_quality: hook_genericness=true.",
                "- [tone] content_dependency: failed section_dependency",
            )
        )
        self.assertEqual(
            _required_semantic_repair_section_ids(script, revision),
            ("s1", "s2", "s3"),
        )

        plan = {
            "title": "وهم الكفاءة",
            "sections": [
                {"id": "s1", "heading": "h1", "purpose": "p1", "visual_query_en": "phone"},
                {"id": "s2", "heading": "h2", "purpose": "p2", "visual_query_en": "feed"},
                {"id": "s3", "heading": "h3", "purpose": "p3", "visual_query_en": "notes"},
            ],
        }
        incomplete = {
            "patches": [
                {
                    "section_id": "s1",
                    "find": "A: هل نتحكم في التقنية أم نتبع ما تعرضه علينا؟",
                    "replace": "A: حين تختار من قائمة رتبتها الشاشة مسبقًا، كم يبقى من اختيارك أنت؟",
                },
                {
                    "section_id": "s3",
                    "find": "B: اكتب فكرتك قبل أن تفتح البحث.",
                    "replace": "B: قارن ما كنت ستختاره قبل فتح الشاشة بما ظهر لك بعدها، ثم راقب أين تغيّر مسارك.",
                },
            ]
        }
        with self.assertRaisesRegex(ValueError, "explicitly flagged section: s2"):
            _validate_and_apply_script_patches(
                incomplete,
                plan=plan,
                original_script=script,
                identity={},
                cta_plan={},
                revision_note=revision,
                allowed_section_ids=("s1", "s2", "s3"),
                required_changed_section_ids=("s1", "s2", "s3"),
            )

    def test_run82_repair_prompt_does_not_claim_failed_dimensions_already_passed(self):
        script = {
            "title": "وهم الكفاءة",
            "sections": [
                {"id": "s1", "narration": "A: هل نتحكم في التقنية أم نتبع ما تعرضه علينا؟"},
                {"id": "s2", "narration": "B: تتكرر أمامك اقتراحات قريبة مما شاهدته سابقًا."},
                {"id": "s3", "narration": "B: اكتب فكرتك قبل أن تفتح البحث."},
            ],
        }
        plan = {
            "title": "وهم الكفاءة",
            "narrative_format": "dialogue_qa",
            "sections": [
                {"id": "s1", "heading": "h1", "purpose": "p1", "visual_query_en": "phone"},
                {"id": "s2", "heading": "h2", "purpose": "p2", "visual_query_en": "feed"},
                {"id": "s3", "heading": "h3", "purpose": "p3", "visual_query_en": "notes"},
            ],
        }
        revision = (
            "- [tone] editorial_promise_continuity:s2 is not earned.\n"
            "- [tone] content_depth:s3 generic action.\n"
            "- [tone] hook_quality: hook_genericness=true, payoff_resolves_hook=false"
        )
        prompt = _tone_repair_prompt(
            brief={"format": "podcast", "research_pack": []},
            plan=plan,
            script=script,
            identity={},
            cta_plan={},
            revision_note=revision,
        )
        self.assertIn("REQUIRED_SEMANTIC_CHANGE_SECTION_IDS", prompt)
        self.assertIn('["s1","s2","s3"]', prompt)
        self.assertIn("SEMANTIC-SPINE REPAIR", prompt)
        self.assertNotIn("current script already PASSED hook_specificity, section_dependency", prompt)
        self.assertIn("Never assume section_dependency", prompt)

    def test_run82_tone_scope_respects_noncausal_research_ceiling(self):
        base = (
            "5. Unverified religious quotations: flag any religious quotation or attribution presented as authoritative unless the\n"
            "   approved research context directly supports it as verified. Judge this semantically - do not rely only on a fixed\n"
            "   list of marker phrases."
        )
        boundaries = (
            "[RESEARCH_BOUNDARIES]\n"
            "market-interest evidence only; does not establish causality or scientific mechanism\n"
            "[/RESEARCH_BOUNDARIES]"
        )
        scoped = _scope_clean_v2_tone_prompt(
            base,
            research_boundaries=boundaries,
        )
        self.assertIn("CLEAN_V2_EVIDENCE_BOUNDARY", scoped)
        self.assertIn("do NOT block merely because the draft lacks such a mechanism", scoped)
        self.assertIn("A text can be deep without pretending evidence exists", scoped)
        self.assertIn("Do not misclassify a finite imperative clause as a masdar fragment", scoped)
        self.assertIn("«فأدخل ...»", scoped)
        self.assertIn(boundaries, scoped)

    def test_run82_podcast_tone_audit_strips_runtime_prayer_before_judgment(self):
        captured = {}
        dummy_plan = SimpleNamespace(
            hook="",
            closing_payoff="",
            identity_opener="",
            identity_closer="",
            identity_transitions=[],
        )
        script = {
            "title": "اختبار",
            "sections": [
                {
                    "id": "s1",
                    "narration": (
                        "A: هل نتحكم فعلًا في التقنية؟ "
                        + PRAYER_SENTENCE
                        + " B: لنفحص ما يحدث حين تسبق الشاشة قرارنا."
                    ),
                },
                {"id": "s2", "narration": "B: هذه فقرة ثانية مرتبطة بالسؤال."},
            ],
        }

        def build(**kwargs):
            captured["audit_script"] = kwargs["script"]
            return dummy_plan

        def audit(_api_key, production_plan, _model, **kwargs):
            captured["research_boundaries"] = kwargs.get("research_boundaries", "")
            return _tone_result()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "narrative-identity.json").write_text(
                json.dumps({"opener": "", "closer": "", "transitions": []}, ensure_ascii=False),
                encoding="utf-8",
            )
            with patch(
                "clean_v2.pipeline._build_production_plan_for_audit",
                side_effect=build,
            ), patch(
                "clean_v2.tone_audit.audit_tone_and_naturalness_with_mistral",
                side_effect=audit,
            ):
                _run_legacy_tone_naturalness_audit(
                    output_dir=root,
                    brief={"format": "podcast", "research_pack": []},
                    plan={"promise": ""},
                    script=script,
                )

        judged = "\n".join(
            item["narration"] for item in captured["audit_script"]["sections"]
        )
        self.assertNotIn(PRAYER_SENTENCE, judged)
        self.assertIn("A: هل نتحكم فعلًا في التقنية؟", judged)

    def test_run256_tone_scope_defers_visual_query_and_respects_host_locks(self):
        base = (
            "5. Unverified religious quotations: flag any religious quotation or attribution presented as authoritative unless the\n"
            "   approved research context directly supports it as verified. Judge this semantically - do not rely only on a fixed\n"
            "   list of marker phrases."
        )
        scoped = _scope_clean_v2_tone_prompt(base)
        self.assertIn("spoken-text audit", scoped)
        self.assertIn("Do not block on visual_query", scoped)
        self.assertIn("contextual CTA anchor section is host-owned", scoped)
        self.assertIn("Do not require moving the CTA to a", scoped)
        self.assertIn("different section or to the ending", scoped)
        self.assertIn("narrative identity opener/closer are host-owned", scoped)

    def test_run254_religious_quote_scope_keeps_invocation_distinct_from_quote(self):
        base = (
            "5. Unverified religious quotations: flag any religious quotation or attribution presented as authoritative unless the\n"
            "   approved research context directly supports it as verified. Judge this semantically - do not rely only on a fixed\n"
            "   list of marker phrases."
        )
        scoped = _scope_religious_quote_prompt(base)
        self.assertIn("بسم الله / باسم الله / حفظكم الله", scoped)
        self.assertIn("are not quotations or attributions by themselves", scoped)
        self.assertIn("unless they actually quote or attribute", scoped)
        self.assertEqual(scoped.count("Scope clarification for Clean V2"), 1)

    def test_short_cohort_attempt_1_factuality_note_resolves_s2(self):
        script = {
            "sections": [
                {
                    "id": "s1",
                    "narration": "أحياناً يختفي الدافع كأنك في منتصف نهارٍ صامت.",
                },
                {
                    "id": "s2",
                    "narration": "لكن الحقيقة أن توقعاتنا للنتيجة تُشغِّل مشاعرنا أكثر من الطاقة نفسها.",
                },
                {
                    "id": "s3",
                    "narration": "ابدأ بتدوين هدف صغير اليوم، ثم اكتب خطوة واحدة لتحقيقه.",
                },
            ]
        }
        report = {
            "status": "block",
            "unsupported_claims": [{"section_id": "s2", "issue": "claim that expectations influence emotions more than energy"}],
            "diagnostics": {"raw_result": {"unsupported_claims": [{"section_id": "s2", "issue": "claim that expectations influence emotions more than energy"}], "professional_advice_flags": [], "expert_persona_flags": []}},
            "professional_advice_flags": [],
            "expert_persona_flags": [],
            "notes": ["Section s2 contains an unsupported psychological claim."],
        }

        factuality_notes = _factuality_repair_issue_notes(report)
        location_notes = _factuality_location_issue_notes(report, script)
        revision_note = "\n".join(
            item for item in (factuality_notes, location_notes) if item
        )

        self.assertEqual(location_notes, "- [factuality-location] s2")
        self.assertEqual(
            _factuality_target_section_ids(report, script),
            ("s2",),
        )

    def test_short_cohort_attempt_1_structured_id_resolves_s2_without_prose_hint(self):
        script = {
            "sections": [
                {
                    "id": "s1",
                    "narration": "لا تنتظر أن يعود الدافع، لأنه لن يأتي بمفرده.",
                },
                {
                    "id": "s2",
                    "narration": "الافتراض الخفي هو أن الحركة تحتاج إلى حماس أولًا، لكن العكس هو الصحيح: الحركة الصغيرة تولد الرغبة، لا العكس.",
                },
                {
                    "id": "s3",
                    "narration": "خذ خطوة واحدة فقط الآن، مثل فتح الكتاب أو كتابة الجملة الأولى، ثم انظر كيف يتغير كل شيء.",
                },
            ]
        }
        report = {
            "status": "block",
            "unsupported_claims": [{"section_id": "s2", "issue": "The claim that small actions generate desire, contrary to the assumption that motivation precedes action."}],
            "diagnostics": {"raw_result": {"unsupported_claims": [{"section_id": "s2", "issue": "The claim that small actions generate desire, contrary to the assumption that motivation precedes action."}], "professional_advice_flags": [], "expert_persona_flags": []}},
            "professional_advice_flags": [],
            "expert_persona_flags": [],
            "notes": [
                "The script contains a psychological claim that action precedes motivation, which is not supported by the empty approved research context."
            ],
        }

        factuality_notes = _factuality_repair_issue_notes(report)
        location_notes = _factuality_location_issue_notes(report, script)
        revision_note = "\n".join(
            item for item in (factuality_notes, location_notes) if item
        )

        self.assertNotIn("s2", "\n".join(report["notes"] + [item["issue"] for item in report["unsupported_claims"]]))
        self.assertEqual(location_notes, "- [factuality-location] s2")
        self.assertEqual(
            _factuality_target_section_ids(report, script),
            ("s2",),
        )

    def test_factuality_provider_block_without_flags_is_local_pass(self):
        script = {
            "sections": [
                {"id": "s1", "narration": "هوك واضح. اللهم صلِّ وسلِّم على نبينا محمد. وهنا في نداء اليقظة، نقترب من أفكار الحياة اليومية بوعيٍ أوضح. ابدأ بخطوة صغيرة."},
                {"id": "s2", "narration": "اختر مهمة واحدة واكتبها الآن."},
                {"id": "s3", "narration": "راجع ما أنجزته في نهاية اليوم."},
            ]
        }
        plan = {"sections": [{"id": "s1"}, {"id": "s2"}, {"id": "s3"}]}
        captured = {}

        def build_plan(*, brief, plan, script):
            captured["script"] = script
            return SimpleNamespace(sections=[])

        def audit(_key, _plan, _research, _model, *, diagnostics):
            diagnostics.update({"validation": "valid", "attempts": [], "raw_result": {"status": "block"}})
            return {
                "status": "block",
                "unsupported_claims": [],
                "professional_advice_flags": [],
                "expert_persona_flags": [],
                "notes": ["provider disagreed without a concrete hard flag"],
            }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "narrative-identity.json").write_text(
                json.dumps({"opener": "", "closer": "", "transitions": []}, ensure_ascii=False),
                encoding="utf-8",
            )
            with patch(
                "clean_v2.pipeline._build_production_plan_for_audit",
                side_effect=build_plan,
            ), patch(
                "clean_v2.text_audit.audit_plan_with_mistral",
                side_effect=audit,
            ):
                report = _run_legacy_factuality_audit(
                    output_dir=root,
                    brief={"format": "short", "research_pack": []},
                    plan=plan,
                    script=script,
                )

        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["provider_status"], "block")
        self.assertEqual(report["decision_source"], "deterministic_local_risk_policy")
        self.assertEqual(report["hard_flag_count"], 0)
        audited = "\n".join(item["narration"] for item in captured["script"]["sections"])
        self.assertNotIn("اللهم صلِّ وسلِّم على نبينا محمد.", audited)
        self.assertNotIn("وهنا في نداء اليقظة، نقترب من أفكار الحياة اليومية بوعيٍ أوضح.", audited)

    def test_factuality_local_flags_block_even_if_provider_status_pass(self):
        plan = {"sections": [{"id": "s1"}]}
        script = {"sections": [{"id": "s1", "narration": "هذه نسبة دقيقة غير موثقة."}]}

        def audit(_key, _plan, _research, _model, *, diagnostics):
            diagnostics.update({"validation": "valid", "attempts": [], "raw_result": {"status": "pass"}})
            return {
                "status": "pass",
                "unsupported_claims": [{"section_id": "s1", "issue": "unsupported statistic"}],
                "professional_advice_flags": [],
                "expert_persona_flags": [],
                "notes": [],
            }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch(
                "clean_v2.pipeline._build_production_plan_for_audit",
                return_value=SimpleNamespace(sections=[]),
            ), patch(
                "clean_v2.text_audit.audit_plan_with_mistral",
                side_effect=audit,
            ):
                with self.assertRaisesRegex(RuntimeError, "factuality/AI-expert gate blocked"):
                    _run_legacy_factuality_audit(
                        output_dir=root,
                        brief={"format": "short", "research_pack": []},
                        plan=plan,
                        script=script,
                    )
            persisted = json.loads((root / "factuality-audit.json").read_text(encoding="utf-8"))
            self.assertEqual(persisted["status"], "block")
            self.assertEqual(persisted["provider_status"], "pass")
            self.assertEqual(persisted["hard_flag_count"], 1)

    def test_factuality_productivity_misflag_is_advisory_not_block(self):
        plan = {"sections": [{"id": "s1"}]}
        script = {"sections": [{"id": "s1", "narration": "اكتب مهمة واحدة وحدد وقتًا لمراجعتها."}]}

        def audit(_key, _plan, _research, _model, *, diagnostics):
            raw = {
                "status": "block",
                "unsupported_claims": [],
                "professional_advice_flags": [
                    {"section_id": "s1", "issue": "ordinary productivity guidance to write one task"}
                ],
                "expert_persona_flags": [],
                "notes": [],
            }
            diagnostics.update({"validation": "valid", "attempts": [], "raw_result": raw})
            return dict(raw)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch(
                "clean_v2.pipeline._build_production_plan_for_audit",
                return_value=SimpleNamespace(sections=[]),
            ), patch(
                "clean_v2.text_audit.audit_plan_with_mistral",
                side_effect=audit,
            ):
                report = _run_legacy_factuality_audit(
                    output_dir=root,
                    brief={"format": "short", "research_pack": []},
                    plan=plan,
                    script=script,
                )

        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["provider_status"], "block")
        self.assertEqual(report["hard_flag_count"], 0)
        self.assertEqual(len(report["advisory_flags"]["professional_advice_flags"]), 1)

    def test_factuality_medical_advice_flag_is_hard_block(self):
        plan = {"sections": [{"id": "s1"}]}
        script = {"sections": [{"id": "s1", "narration": "غيّر جرعة الدواء لعلاج الأعراض."}]}

        def audit(_key, _plan, _research, _model, *, diagnostics):
            raw = {
                "status": "pass",
                "unsupported_claims": [],
                "professional_advice_flags": [
                    {"section_id": "s1", "issue": "medical treatment advice"}
                ],
                "expert_persona_flags": [],
                "notes": [],
            }
            diagnostics.update({"validation": "valid", "attempts": [], "raw_result": raw})
            return dict(raw)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch(
                "clean_v2.pipeline._build_production_plan_for_audit",
                return_value=SimpleNamespace(sections=[]),
            ), patch(
                "clean_v2.text_audit.audit_plan_with_mistral",
                side_effect=audit,
            ):
                with self.assertRaisesRegex(RuntimeError, "factuality/AI-expert gate blocked"):
                    _run_legacy_factuality_audit(
                        output_dir=root,
                        brief={"format": "short", "research_pack": []},
                        plan=plan,
                        script=script,
                    )
            persisted = json.loads((root / "factuality-audit.json").read_text(encoding="utf-8"))
            self.assertEqual(persisted["status"], "block")
            self.assertEqual(persisted["hard_flag_count"], 1)

    def test_valid_content_block_is_quality_block_not_infrastructure(self):
        blocked = _tone_result(status="block")
        blocked["naturalness_flags"] = ["generic AI filler"]
        dummy_plan = SimpleNamespace(hook="")

        with tempfile.TemporaryDirectory() as tmp, patch(
            "clean_v2.pipeline._build_production_plan_for_audit",
            return_value=dummy_plan,
        ), patch(
            "clean_v2.tone_audit.audit_tone_and_naturalness_with_mistral",
            return_value=blocked,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "tone/naturalness gate blocked real production",
            ):
                _run_legacy_tone_naturalness_audit(
                    output_dir=Path(tmp),
                    brief={"format": "film"},
                    plan={"sections": []},
                    script={
                        "sections": [
                            {"id": "s1", "narration": "افتتاح واضح. ثم شرح طبيعي."}
                        ]
                    },
                )
            persisted = json.loads(
                (Path(tmp) / "tone-naturalness-audit.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(persisted["status"], "block")
            self.assertEqual(dummy_plan.hook, "افتتاح واضح.")

    def test_content_block_prints_a_stdout_diagnostic(self):
        # tone-naturalness-audit.json lives only in the uploaded artifact (Azure
        # Blob), unreachable from network-restricted environments. This mirrors
        # the same failing flags/notes to stdout (job logs are always reachable)
        # so a future block is diagnosable without the artifact.
        blocked = _tone_result(status="block")
        blocked["narrative_format_flags"] = ["hook_quality:payoff_resolves_hook"]
        blocked["payoff_resolves_hook"] = False
        blocked["notes"] = ["Hook opens a question the payoff never answers."]
        dummy_plan = SimpleNamespace(hook="")

        with tempfile.TemporaryDirectory() as tmp, patch(
            "clean_v2.pipeline._build_production_plan_for_audit",
            return_value=dummy_plan,
        ), patch(
            "clean_v2.tone_audit.audit_tone_and_naturalness_with_mistral",
            return_value=blocked,
        ):
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured), self.assertRaisesRegex(
                RuntimeError, "tone/naturalness gate blocked real production"
            ):
                _run_legacy_tone_naturalness_audit(
                    output_dir=Path(tmp),
                    brief={"format": "podcast"},
                    plan={"sections": []},
                    script={
                        "sections": [
                            {"id": "s1", "narration": "افتتاح واضح. ثم شرح طبيعي."}
                        ]
                    },
                )

        printed = captured.getvalue()
        self.assertIn("Clean V2 tone/naturalness block diagnostic: ", printed)
        diagnostic_line = next(
            line
            for line in printed.splitlines()
            if line.startswith("Clean V2 tone/naturalness block diagnostic: ")
        )
        diagnostic = json.loads(
            diagnostic_line.removeprefix("Clean V2 tone/naturalness block diagnostic: ")
        )
        self.assertEqual(
            diagnostic["narrative_format_flags"],
            ["hook_quality:payoff_resolves_hook"],
        )
        self.assertFalse(diagnostic["payoff_resolves_hook"])
        self.assertEqual(
            diagnostic["notes"], ["Hook opens a question the payoff never answers."]
        )

    def test_passing_audit_prints_an_editorial_voice_advisory_line(self):
        # Observation only, fires on every real audit (pass or block), unlike
        # the block-only diagnostic above - this is how filler/unearned-
        # payoff/cold-open frequency gets watched across normal runs before
        # any decision to turn it into a real gate.
        passing = _tone_result()
        passing["filler_flags"] = ["جملة انتقالية فارغة."]
        passing["payoff_earned"] = False
        passing["cold_open_story_violation"] = True
        dummy_plan = SimpleNamespace(hook="")

        with tempfile.TemporaryDirectory() as tmp, patch(
            "clean_v2.pipeline._build_production_plan_for_audit",
            return_value=dummy_plan,
        ), patch(
            "clean_v2.tone_audit.audit_tone_and_naturalness_with_mistral",
            return_value=passing,
        ):
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured):
                report = _run_legacy_tone_naturalness_audit(
                    output_dir=Path(tmp),
                    brief={"format": "short"},
                    plan={"sections": []},
                    script={
                        "sections": [
                            {"id": "s1", "narration": "افتتاح واضح. ثم شرح طبيعي."}
                        ]
                    },
                )

        self.assertEqual(report["status"], "pass")
        printed = captured.getvalue()
        self.assertIn("Clean V2 editorial voice advisory: ", printed)
        advisory_line = next(
            line
            for line in printed.splitlines()
            if line.startswith("Clean V2 editorial voice advisory: ")
        )
        advisory = json.loads(
            advisory_line.removeprefix("Clean V2 editorial voice advisory: ")
        )
        self.assertEqual(advisory["status"], "pass")
        self.assertEqual(advisory["filler_flags"], ["جملة انتقالية فارغة."])
        self.assertIs(advisory["payoff_earned"], False)
        self.assertIs(advisory["cold_open_story_violation"], True)

    def test_provider_exhaustion_is_advisory_not_content_block(self):
        exhausted = _tone_result(status="block", validation="providers_exhausted")
        exhausted["attempts"] = [
            {"provider": "gemini", "outcome": "rate_limited"},
            {"provider": "groq", "outcome": "rate_limited"},
            {"provider": "openrouter", "outcome": "other"},
            {"provider": "mistral", "outcome": "rate_limited"},
        ]

        with tempfile.TemporaryDirectory() as tmp, patch(
            "clean_v2.pipeline._build_production_plan_for_audit",
            return_value=SimpleNamespace(hook=""),
        ), patch(
            "clean_v2.tone_audit.audit_tone_and_naturalness_with_mistral",
            return_value=exhausted,
        ):
            report = _run_legacy_tone_naturalness_audit(
                output_dir=Path(tmp),
                brief={"format": "film"},
                plan={"sections": []},
                script={"sections": [{"id": "s1", "narration": "افتتاح واضح."}]},
            )

            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["audit_availability"], "unavailable")
            self.assertTrue(report["advisory_only"])
            persisted = json.loads(
                (Path(tmp) / "tone-naturalness-audit.json").read_text(encoding="utf-8")
            )
            self.assertEqual(persisted["status"], "pass")
            self.assertEqual(
                persisted["decision_source"],
                "deterministic_provider_availability_policy",
            )

    def test_run15_no_effect_tone_repair_fails_closed_before_reaudit(self):
        script = {
            "sections": [
                {"id": "s1", "narration": "أجلس وأنتظر الدافع."},
                {"id": "s2", "narration": "أقول لنفسي إن الحركة مؤجلة."},
                {"id": "s3", "narration": "أفتح الدفتر وأكتب كلمة واحدة."},
            ]
        }
        original = json.loads(json.dumps(script, ensure_ascii=False))

        class NoOpRouter:
            def route(self, **_kwargs):
                return json.loads(json.dumps(original, ensure_ascii=False))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "narrative-identity.json").write_text(
                json.dumps({"opener": "", "closer": "", "transitions": []}, ensure_ascii=False),
                encoding="utf-8",
            )
            (root / "cta-plan.json").write_text(
                json.dumps({"mode": "none", "spoken_text": ""}, ensure_ascii=False),
                encoding="utf-8",
            )
            with patch(
                "clean_v2.pipeline._tone_repair_issue_notes",
                return_value="- [tone] inner dialogue still sounds external",
            ), patch(
                "clean_v2.pipeline._structural_repair_issue_notes",
                return_value="",
            ), patch(
                "clean_v2.pipeline._short_template_tone_repair_issue_notes",
                return_value="",
            ), patch(
                "clean_v2.pipeline._repair_target_section_ids",
                return_value=("s1", "s2", "s3"),
            ):
                with self.assertRaisesRegex(RuntimeError, "TONE_REPAIR_NO_EFFECT"):
                    _run_one_bounded_tone_repair(
                        output_dir=root,
                        brief={"format": "short"},
                        plan={"sections": []},
                        script=script,
                        router=NoOpRouter(),
                        blocked_report={"status": "block"},
                    )

            self.assertEqual(script, original)
            candidate = json.loads(
                (root / "script-post-tone-repair.json").read_text(encoding="utf-8")
            )
            self.assertEqual(candidate, original)
            report = json.loads(
                (root / "tone-repair.json").read_text(encoding="utf-8")
            )
            self.assertEqual(report["status"], "failed_closed")
            self.assertEqual(report["reason"], "TONE_REPAIR_NO_EFFECT")
            self.assertFalse(report["narration_changed"])

    def test_tone_repair_guard_allows_real_narration_change(self):
        script = {
            "sections": [
                {"id": "s1", "narration": "أجلس وأنتظر الدافع."},
                {"id": "s2", "narration": "أقول لنفسي إن الحركة مؤجلة."},
                {"id": "s3", "narration": "أفتح الدفتر وأكتب كلمة واحدة."},
            ]
        }
        repaired = json.loads(json.dumps(script, ensure_ascii=False))
        repaired["sections"][1]["narration"] = "لاحظت أنني أؤجل الحركة وأنا أنتظر شعورًا قد لا يأتي."

        class ChangedRouter:
            def route(self, **_kwargs):
                return json.loads(json.dumps(repaired, ensure_ascii=False))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "narrative-identity.json").write_text(
                json.dumps({"opener": "", "closer": "", "transitions": []}, ensure_ascii=False),
                encoding="utf-8",
            )
            (root / "cta-plan.json").write_text(
                json.dumps({"mode": "none", "spoken_text": ""}, ensure_ascii=False),
                encoding="utf-8",
            )
            with patch(
                "clean_v2.pipeline._tone_repair_issue_notes",
                return_value="- [tone] inner dialogue still sounds external",
            ), patch(
                "clean_v2.pipeline._structural_repair_issue_notes",
                return_value="",
            ), patch(
                "clean_v2.pipeline._short_template_tone_repair_issue_notes",
                return_value="",
            ), patch(
                "clean_v2.pipeline._repair_target_section_ids",
                return_value=("s2",),
            ), patch(
                "clean_v2.pipeline._assert_brand_signature_invariant",
                return_value=None,
            ):
                report = _run_one_bounded_tone_repair(
                    output_dir=root,
                    brief={"format": "short"},
                    plan={"sections": []},
                    script=script,
                    router=ChangedRouter(),
                    blocked_report={"status": "block"},
                )

            self.assertTrue(report["narration_changed"])
            self.assertEqual(script["sections"][1]["narration"], repaired["sections"][1]["narration"])

    def test_composite_runs_factuality_before_tone(self):
        order = []

        def factuality(**kwargs):
            order.append("factuality")
            return {"status": "pass"}

        def tone(**kwargs):
            order.append("tone")
            return {"status": "pass"}

        with patch(
            "clean_v2.pipeline._run_legacy_factuality_audit",
            side_effect=factuality,
        ), patch(
            "clean_v2.pipeline._run_legacy_tone_naturalness_audit",
            side_effect=tone,
        ):
            report = _run_text_audits(
                output_dir=Path("."),
                brief={},
                plan={},
                script={},
            )

        self.assertEqual(order, ["factuality", "tone"])
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["factuality_status"], "pass")
        self.assertEqual(report["tone_naturalness_status"], "pass")


class EditorialVoiceWriterPromptTests(unittest.TestCase):
    """Closing the loop: the writer's own generation prompt now carries the
    same HUMAN_VOICE_NO_FILLER/COLD_OPEN_AS_SCENE guidance the (advisory-only,
    non-blocking) tone audit separately observes - not just a rule the writer
    is silently measured against after the fact.
    """

    SHORT_COLD_OPEN_ELIGIBLE = ("inner_dialogue", "micro_story")
    SHORT_COLD_OPEN_INELIGIBLE = ("why_reframe", "quote_reflection")
    FILM_COLD_OPEN_ELIGIBLE = (
        "direct_cinematic",
        "inner_dialogue",
        "story_analysis",
        "paradox",
        "hypothesis_test",
    )
    FILM_COLD_OPEN_INELIGIBLE = (
        "question_answer",
        "dialogue_qa",
        "problem_reveal_solution",
        "connected_list",
    )

    def test_all_four_short_templates_carry_human_voice_no_filler(self):
        self.assertEqual(set(TEMPLATE_WRITING_DIRECTIVES), {
            "why_reframe", "inner_dialogue", "micro_story", "quote_reflection",
        })
        for template, directive in TEMPLATE_WRITING_DIRECTIVES.items():
            self.assertIn(HUMAN_VOICE_NO_FILLER, directive, template)

    def test_cold_open_as_scene_is_scoped_to_narrative_short_templates_only(self):
        for template in self.SHORT_COLD_OPEN_ELIGIBLE:
            self.assertIn(COLD_OPEN_AS_SCENE, TEMPLATE_WRITING_DIRECTIVES[template], template)
        for template in self.SHORT_COLD_OPEN_INELIGIBLE:
            self.assertNotIn(COLD_OPEN_AS_SCENE, TEMPLATE_WRITING_DIRECTIVES[template], template)

    def test_all_nine_film_profiles_carry_human_voice_no_filler(self):
        self.assertEqual(
            set(_LONGFORM_PROFILES),
            set(self.FILM_COLD_OPEN_ELIGIBLE) | set(self.FILM_COLD_OPEN_INELIGIBLE),
        )
        for name, profile in _LONGFORM_PROFILES.items():
            self.assertIn(HUMAN_VOICE_NO_FILLER, profile["writing"], name)

    def test_cold_open_as_scene_is_scoped_to_narrative_film_profiles_only(self):
        for name in self.FILM_COLD_OPEN_ELIGIBLE:
            self.assertIn(COLD_OPEN_AS_SCENE, _LONGFORM_PROFILES[name]["writing"], name)
        for name in self.FILM_COLD_OPEN_INELIGIBLE:
            self.assertNotIn(COLD_OPEN_AS_SCENE, _LONGFORM_PROFILES[name]["writing"], name)

    def test_podcast_fixed_profile_carries_no_filler_but_never_cold_open(self):
        # Podcast's one fixed house style is itself a real listener question
        # answered directly - forcing a scene-open would contradict its own
        # defined purpose, per the earlier design discussion.
        writing = _PODCAST_FIXED_PROFILE["writing"]
        self.assertIn(HUMAN_VOICE_NO_FILLER, writing)
        self.assertNotIn(COLD_OPEN_AS_SCENE, writing)


if __name__ == "__main__":
    unittest.main()

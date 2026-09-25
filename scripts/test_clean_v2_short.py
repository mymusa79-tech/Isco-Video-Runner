from __future__ import annotations

import inspect
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from clean_v2.contextual_cta import CtaMode, bind_contextual_cta
from clean_v2.contracts import ContractError, validate_plan
from clean_v2.opening_director import run_opening_director
from clean_v2.pipeline import (
    CleanV2Pipeline,
    _audit_narrative_format_for_brief,
    _planning_prompt,
    _script_prompt,
    _short_identity_not_applicable,
    _synthesize_sectioned_voice,
    _validate_script_for_brief,
)
from clean_v2 import media as media_module
from clean_v2 import visual_qa as visual_qa_module
from clean_v2.media import (
    GeminiPrimaryNabraFallbackSynthesizer,
    GeminiPrimaryPiperFallbackSynthesizer,
    SHORT_CHARON_STYLE,
    SHORT_CUT_DISSOLVE_SECONDS,
    SHORT_MASTER_LOOK_FILTER,
    SHORT_MIN_COLOR_SATURATION_AVG,
    SHORT_STOCK_ASSET_MAX,
    SHORT_VISUAL_MAX,
    SHORT_VISUAL_MIN,
    SHORT_VISUAL_TARGET,
    StockVisualSource,
    _expand_short_visual_sequence,
    VoiceInfrastructureError,
)
from clean_v2.providers import ProviderAdapter, ProviderRouter, _safe_validator_reason
from clean_v2.audio_mastering import CHARON_CORRECTIVE_FILTER, CHARON_CORRECTIVE_PROFILE
from clean_v2.short_audio_polish import (
    MUSIC_MAX_REL_DB,
    MUSIC_MIN_REL_DB,
    MUSIC_TARGET_REL_DB,
    SFX_MAX_REL_DB,
    SFX_MIN_REL_DB,
    SFX_TARGET_REL_DB,
    _generate_raw_music,
    _generate_raw_sfx,
    _measure_mean_db,
    _normalize_relative,
    apply_short_audio_polish,
)
from clean_v2.short_timed_text import (
    ACCENT_ASS,
    BODY_FONT,
    BODY_FONT_SIZE,
    CAPTION_MAX_WORDS,
    CAPTION_MIN_WORDS,
    FOCUS_FONT,
    FOCUS_FONT_SIZE,
    MAX_DARK_SLATES,
    build_events_from_voice_timeline,
    build_rich_ass,
    choose_dark_slate_index,
    split_focus_phrase,
    validate_progressive_text,
)
from clean_v2 import short_voice_owned_timeline as voice_timeline_module
from clean_v2.short_voice_owned_timeline import (
    ShortVoiceTimelineError,
    build_short_voice_owned_timeline,
    retime_events,
    section_duration_map,
)
from clean_v2.short_format import (
    SHORT_HEIGHT,
    SHORT_HOOK_MAX_WORDS,
    SHORT_DURATION_SAFETY_MAX_SECONDS,
    SHORT_SECTION_COUNT,
    SHORT_WIDTH,
    ShortFormatError,
    TEMPLATE_VISUAL_QUERY_DIRECTIVES,
    apply_safe_short_s3_single_action_trim,
    select_short_template,
    short_contract_report,
    short_prompt_context,
    validate_short_dimensions,
    validate_short_duration,
    validate_short_hook_contract,
    validate_short_script,
    validate_short_visual_queries,
)


def _brief(topic: str, *, editorial_intent: str = "", research_pack=None) -> dict:
    return {
        "approved_by_user": True,
        "approved_topic": topic,
        "format": "short",
        "language": "ar",
        "audience": "Arabic-speaking adults",
        "editorial_intent": editorial_intent or "نبرة هادئة وعملية وطبيعية.",
        "research_pack": list(research_pack or []),
        "hard_constraints": ["No fabricated facts."],
    }


def _plan(queries: list[str]) -> dict:
    return {
        "title": "شورت تجريبي",
        "promise": "فكرة واحدة واضحة وقابلة للتطبيق",
        "cta": "",
        "sections": [
            {
                "id": f"s{index}",
                "heading": f"قسم {index}",
                "purpose": f"غرض القسم {index}",
                "visual_query_en": query,
                "visual_query_alt_en": f"{query} close detail",
            }
            for index, query in enumerate(queries, 1)
        ],
    }


_TEMPLATE_FIXTURES = {
    "why_reframe": {
        "brief": _brief("لماذا نخطئ عندما نظن أن الخطة المثالية تكفي؟"),
        "queries": [
            "confused worker staring at cluttered daily schedule",
            "person pause reconsidering written plan at desk",
            "calm focused worker using simple practical schedule",
        ],
    },
    "inner_dialogue": {
        "brief": _brief("كيف تنهض عندما تفقد الدافع وتقول لنفسك لا أستطيع؟"),
        "queries": [
            "person alone thoughtful quiet moment by window",
            "solitary person thinking in calm quiet room",
            "reflective person walking alone peaceful morning",
        ],
    },
    "micro_story": {
        "brief": _brief("قصة قصيرة: ذات يوم بدأت تجربة صغيرة ثم تغيرت النتيجة"),
        "queries": [
            "woman opening notebook at quiet desk",
            "woman writing notebook task list at desk",
            "woman closing notebook after finishing work",
        ],
    },
    "quote_reflection": {
        "brief": _brief("اقتباس للتأمل: «ابدأ بما تستطيع اليوم»"),
        "queries": [
            "quiet reflective room with soft morning light",
            "calm person reading slowly in minimal room",
            "peaceful contemplative window scene with still light",
        ],
    },
}


class ShortTemplateSelectionTests(unittest.TestCase):
    def test_all_four_templates_are_reachable_deterministically(self) -> None:
        for expected, fixture in _TEMPLATE_FIXTURES.items():
            with self.subTest(template=expected):
                selection = select_short_template(fixture["brief"])
                self.assertEqual(selection["template"], expected)
                self.assertEqual(selection["extra_ai_calls"], 0)
                self.assertIn(expected, TEMPLATE_VISUAL_QUERY_DIRECTIVES)

    def test_short_tone_audit_uses_selected_template_for_all_four_formats(self) -> None:
        # inner_dialogue is relabeled for the Engine's reused audit only (Runs
        # #17/#22: its own name misled the audit into expecting an actual
        # two-voice exchange). Every other template name passes through as-is.
        audit_overrides = {"inner_dialogue": "inner_monologue"}
        for expected, fixture in _TEMPLATE_FIXTURES.items():
            with self.subTest(template=expected):
                brief = fixture["brief"]
                self.assertEqual(select_short_template(brief)["template"], expected)
                self.assertEqual(
                    _audit_narrative_format_for_brief(brief),
                    audit_overrides.get(expected, expected),
                )

        film = dict(_TEMPLATE_FIXTURES["inner_dialogue"]["brief"])
        film["format"] = "film"
        self.assertEqual(_audit_narrative_format_for_brief(film), "direct_cinematic")

    def test_quote_reflection_requires_real_quote_evidence(self) -> None:
        brief = _brief("هذه عبارة جميلة للتأمل")
        selection = select_short_template(brief)
        self.assertNotEqual(selection["template"], "quote_reflection")

    def test_template_specific_visual_queries_are_enforced(self) -> None:
        for expected, fixture in _TEMPLATE_FIXTURES.items():
            with self.subTest(template=expected):
                plan = _plan(fixture["queries"])
                report = validate_short_visual_queries(plan, fixture["brief"])
                self.assertEqual(report["template"], expected)
                self.assertEqual(report["status"], "pass")
                self.assertEqual(len(report["queries"]), 3)

    def test_why_reframe_rejects_repeating_same_writing_action_for_payoff(self) -> None:
        fixture = _TEMPLATE_FIXTURES["why_reframe"]
        repeated = _plan(
            [
                "frustrated person writing messy notes at desk",
                "person pause reconsidering plan while standing",
                "calm person writing simple note at desk",
            ]
        )
        with self.assertRaisesRegex(
            ShortFormatError,
            "payoff_repeats_opening_action",
        ):
            validate_short_visual_queries(repeated, fixture["brief"])

    def test_cohort6_inner_dialogue_rejects_notebook_opening_and_notebook_payoff(self) -> None:
        fixture = _TEMPLATE_FIXTURES["inner_dialogue"]
        cohort6_shape = _plan(
            [
                "person sitting alone at wooden table hands still looking at empty notebook and pen early morning light",
                "close-up of hands holding a half-empty glass of water person hesitating before taking a sip quiet indoor setting",
                "quiet person writing one word in notebook then closing it with a slight smile hands resting on the page",
            ]
        )
        with self.assertRaisesRegex(
            ShortFormatError,
            "inner_dialogue_payoff_repeats_opening_action",
        ):
            validate_short_visual_queries(cohort6_shape, fixture["brief"])

    def test_inner_dialogue_rejects_generic_visual_queries(self) -> None:
        fixture = _TEMPLATE_FIXTURES["inner_dialogue"]
        generic = _plan(
            [
                "desk calendar and coffee cup",
                "city street wide establishing shot",
                "generic office laptop workspace",
            ]
        )
        with self.assertRaisesRegex(
            ShortFormatError,
            "inner_dialogue_hook_not_readable",
        ):
            validate_short_visual_queries(generic, fixture["brief"])

    def test_planning_prompt_contains_selected_template_visual_direction_without_extra_call(self) -> None:
        for expected, fixture in _TEMPLATE_FIXTURES.items():
            with self.subTest(template=expected):
                prompt = _planning_prompt(fixture["brief"])
                selection = select_short_template(fixture["brief"])
                self.assertIn("Use exactly 3 sections", prompt)
                self.assertIn("VISUAL_QUERY_DIRECTION", prompt)
                self.assertIn(selection["visual_query_directive"], prompt)
                self.assertIn("visual_query_alt_en", prompt)
                self.assertIn("TWO distinct visual intents", prompt)
                self.assertIn("visibly different dominant actions or states", prompt)
                self.assertIn("scroll-stop visual beat", prompt)
                self.assertIn("must not feel visually flat", prompt)
                if expected == "why_reframe":
                    self.assertIn("visually unresolved", prompt)
                elif expected == "inner_dialogue":
                    self.assertIn("do NOT make the hook visually calm", prompt)
                elif expected == "micro_story":
                    self.assertIn("action or event already in motion", prompt)
                elif expected == "quote_reflection":
                    self.assertIn("visually arresting through composition rather than frantic motion", prompt)
                self.assertIn(f"selected_template={expected}", prompt)
                self.assertIn("return an empty CTA string", prompt)
                self.assertEqual(selection["extra_ai_calls"], 0)


class ShortProviderDiagnosticsTests(unittest.TestCase):
    def test_shortformaterror_persists_only_safe_rule_code(self) -> None:
        reason = _safe_validator_reason(
            ShortFormatError("short_s3_requires_one_action_only imperative_markers=2")
        )
        self.assertEqual(
            reason,
            "invalid_output_shortformaterror_short_s3_requires_one_action_only",
        )

    def test_non_short_validator_error_keeps_generic_reason(self) -> None:
        self.assertEqual(
            _safe_validator_reason(ValueError("sensitive rejected text")),
            "invalid_output_valueerror",
        )


class ShortContractTests(unittest.TestCase):
    def test_plan_requires_exactly_three_sections_and_empty_social_cta(self) -> None:
        brief = _TEMPLATE_FIXTURES["inner_dialogue"]["brief"]
        valid = _plan(_TEMPLATE_FIXTURES["inner_dialogue"]["queries"])
        normalized = validate_plan(valid, brief)
        self.assertEqual(len(normalized["sections"]), SHORT_SECTION_COUNT)
        self.assertEqual(normalized["cta"], "")

        too_short = _plan(valid["sections"] and _TEMPLATE_FIXTURES["inner_dialogue"]["queries"][:2])
        with self.assertRaisesRegex(ContractError, "exactly 3 for short"):
            validate_plan(too_short, brief)

        with_cta = _plan(_TEMPLATE_FIXTURES["inner_dialogue"]["queries"])
        with_cta["cta"] = "اشترك في القناة"
        with self.assertRaisesRegex(ContractError, "empty social cta"):
            validate_plan(with_cta, brief)

    def test_script_contract_requires_hook_single_voice_and_zero_social_cta(self) -> None:
        valid = {
            "title": "شورت",
            "sections": [
                {"id": "s1", "narration": "قد لا تكون المشكلة في الدافع نفسه. حين تتوقف قليلًا ترى ما يحدث بوضوح."},
                {"id": "s2", "narration": "الفكرة الصغيرة هنا أن تلاحظ اللحظة التي تنسحب فيها من الفعل، دون لوم أو مبالغة."},
                {"id": "s3", "narration": "اختر حركة بسيطة تستطيع تنفيذها الآن، ثم دع الخطوة التالية تأتي بعد أن تبدأ."},
            ],
        }
        report = validate_short_script(valid)
        self.assertTrue(report["single_voice"])
        self.assertFalse(report["social_cta"])
        self.assertLessEqual(report["hook_words"], SHORT_HOOK_MAX_WORDS)

        dialogue = json.loads(json.dumps(valid, ensure_ascii=False))
        dialogue["sections"][0]["narration"] = "A: هل أبدأ الآن؟ B: نعم، بخطوة واحدة واضحة."
        with self.assertRaisesRegex(ShortFormatError, "single_voice"):
            validate_short_script(dialogue)

        cta = json.loads(json.dumps(valid, ensure_ascii=False))
        cta["sections"][2]["narration"] += " اشترك في القناة."
        with self.assertRaisesRegex(ShortFormatError, "zero_social_cta"):
            validate_short_script(cta)

    def test_cohort_attempt_2_s3_requires_one_direct_practical_action(self) -> None:
        prompt = short_prompt_context(_TEMPLATE_FIXTURES["inner_dialogue"]["brief"])
        self.assertIn("MUST begin with a direct Arabic imperative verb", prompt)
        self.assertIn("STRICTER SAFEGUARD", prompt)
        self.assertIn("SELF-CHECK", prompt)
        for example in ("ابدأ بمهمة واحدة", "جرّب أن", "افعل شيئًا واحدًا", "اختر مهمة واحدة", "اكتب أول خطوة"):
            self.assertIn(example, prompt)

        no_action = {
            "title": "شورت",
            "sections": [
                {"id": "s1", "narration": "قد يختفي الدافع حين تنتظر الشعور قبل أن تبدأ."},
                {"id": "s2", "narration": "أحيانًا نربط البداية بالشعور المناسب، فنؤجل الحركة نفسها."},
                {"id": "s3", "narration": "الخطوة الصغيرة الآن قد تكون أقرب طريق للخروج من الانتظار."},
            ],
        }
        with self.assertRaisesRegex(
            ShortFormatError,
            r"short_s3_requires_exactly_one_practical_action action_sentences=0",
        ):
            validate_short_script(no_action)

        direct_action = json.loads(json.dumps(no_action, ensure_ascii=False))
        direct_action["sections"][2]["narration"] = (
            "عندها يصبح الطريق أوضح. اكتب أول خطوة تستطيع تنفيذها الآن."
        )
        report = validate_short_script(direct_action)
        self.assertEqual(report["practical_action_sentences"], 1)
        self.assertEqual(report["practical_action_markers"], 1)

        prefixed_action = json.loads(json.dumps(no_action, ensure_ascii=False))
        prefixed_action["sections"][2]["narration"] = "الآن ابدأ بخطوة صغيرة."
        with self.assertRaisesRegex(
            ShortFormatError,
            r"short_s3_action_must_begin_with_direct_imperative",
        ):
            validate_short_script(prefixed_action)

        payoff_derivative = json.loads(json.dumps(no_action, ensure_ascii=False))
        payoff_derivative["sections"][2]["narration"] = (
            "بدأت الصورة تتضح عندما قلّ الضغط. اكتب خطوة واحدة واضحة."
        )
        with self.assertRaisesRegex(
            ShortFormatError,
            r"short_s3_payoff_contains_forbidden_action_family",
        ):
            validate_short_script(payoff_derivative)

        double_action = json.loads(json.dumps(no_action, ensure_ascii=False))
        double_action["sections"][2]["narration"] = "اكتب كلمة واحدة على ورقة ثم اخرج للمشي."
        with self.assertRaisesRegex(
            ShortFormatError,
            r"short_s3_requires_one_action_only imperative_markers=2",
        ):
            validate_short_script(double_action)

    def test_safe_s3_action_trim_preserves_payoff_and_strict_validator(self) -> None:
        base = {
            "title": "شورت",
            "sections": [
                {"id": "s1", "narration": "قد يختفي الدافع حين تنتظر الشعور قبل أن تبدأ."},
                {"id": "s2", "narration": "أحيانًا نربط البداية بالشعور المناسب فنؤجل الحركة نفسها."},
                {
                    "id": "s3",
                    "narration": "عندها يصبح الطريق أوضح. اكتب كلمة واحدة على ورقة، ثم اخرج للمشي.",
                },
            ],
        }
        self.assertTrue(apply_safe_short_s3_single_action_trim(base))
        self.assertEqual(
            base["sections"][2]["narration"],
            "عندها يصبح الطريق أوضح. اكتب كلمة واحدة على ورقة.",
        )
        self.assertEqual(validate_short_script(base)["practical_action_markers"], 1)

        two_sentences = json.loads(json.dumps(base, ensure_ascii=False))
        two_sentences["sections"][2]["narration"] = (
            "عندها يصبح الطريق أوضح. اختر مهمة واحدة الآن. اكتب أول خطوة فقط."
        )
        self.assertTrue(apply_safe_short_s3_single_action_trim(two_sentences))
        self.assertEqual(
            two_sentences["sections"][2]["narration"],
            "عندها يصبح الطريق أوضح. اكتب أول خطوة فقط.",
        )
        validate_short_script(two_sentences)

        prefixed_extra_action = json.loads(json.dumps(base, ensure_ascii=False))
        prefixed_extra_action["sections"][2]["narration"] = (
            "عندها يصبح الطريق أوضح. اختر مهمة واحدة الآن. ثم ابدأ بها فورًا."
        )
        self.assertTrue(apply_safe_short_s3_single_action_trim(prefixed_extra_action))
        self.assertEqual(
            prefixed_extra_action["sections"][2]["narration"],
            "عندها يصبح الطريق أوضح. اختر مهمة واحدة الآن.",
        )
        validate_short_script(prefixed_extra_action)

        no_safe_payoff = json.loads(json.dumps(base, ensure_ascii=False))
        no_safe_payoff["sections"][2]["narration"] = (
            "اختر مهمة واحدة الآن. ثم ابدأ بها فورًا."
        )
        original = no_safe_payoff["sections"][2]["narration"]
        self.assertFalse(apply_safe_short_s3_single_action_trim(no_safe_payoff))
        self.assertEqual(no_safe_payoff["sections"][2]["narration"], original)

        unsafe = json.loads(json.dumps(base, ensure_ascii=False))
        unsafe["sections"][2]["narration"] = (
            "عندها يصبح الطريق أوضح. اكتب كلمة واحدة اخرج للمشي."
        )
        original = unsafe["sections"][2]["narration"]
        self.assertFalse(apply_safe_short_s3_single_action_trim(unsafe))
        self.assertEqual(unsafe["sections"][2]["narration"], original)
        with self.assertRaisesRegex(ShortFormatError, "short_s3_requires_one_action_only"):
            validate_short_script(unsafe)

        action_only = json.loads(json.dumps(base, ensure_ascii=False))
        action_only["sections"][2]["narration"] = "اكتب كلمة واحدة، ثم اخرج للمشي."
        self.assertFalse(apply_safe_short_s3_single_action_trim(action_only))

        forbidden_payoff = json.loads(json.dumps(base, ensure_ascii=False))
        forbidden_payoff["sections"][2]["narration"] = (
            "عندما يخف الضغط تصبح الصورة أوضح. "
            "البداية الصغيرة تكسر الجمود. "
            "اختر مهمة واحدة الآن."
        )
        self.assertTrue(apply_safe_short_s3_single_action_trim(forbidden_payoff))
        self.assertEqual(
            forbidden_payoff["sections"][2]["narration"],
            "عندما يخف الضغط تصبح الصورة أوضح. اختر مهمة واحدة الآن.",
        )
        validate_short_script(forbidden_payoff)

        only_forbidden_payoff = json.loads(json.dumps(base, ensure_ascii=False))
        only_forbidden_payoff["sections"][2]["narration"] = (
            "البداية الصغيرة تكسر الجمود. اختر مهمة واحدة الآن."
        )
        original = only_forbidden_payoff["sections"][2]["narration"]
        self.assertFalse(
            apply_safe_short_s3_single_action_trim(only_forbidden_payoff)
        )
        self.assertEqual(only_forbidden_payoff["sections"][2]["narration"], original)
        with self.assertRaisesRegex(
            ShortFormatError,
            "short_s3_payoff_contains_forbidden_action_family",
        ):
            validate_short_script(only_forbidden_payoff)

    def test_pipeline_reapplies_safe_s3_trim_after_text_repair(self) -> None:
        source = inspect.getsource(CleanV2Pipeline.run)
        post_repair = source.split(
            "# A successful bounded repair mutates script.json in place.",
            1,
        )[1].split("identity_runtime =", 1)[0]
        trim_index = post_repair.index("apply_safe_short_s3_single_action_trim(script)")
        validate_index = post_repair.index("validate_short_script(script)")
        self.assertLess(trim_index, validate_index)

    def test_pipeline_applies_safe_s3_action_trim_before_acceptance(self) -> None:
        brief = _TEMPLATE_FIXTURES["inner_dialogue"]["brief"]
        plan = _plan(_TEMPLATE_FIXTURES["inner_dialogue"]["queries"])
        value = {
            "title": "شورت",
            "sections": [
                {
                    "id": "s1",
                    "narration": "قد يختفي الدافع حين تنتظر الشعور قبل أن تبدأ.",
                },
                {
                    "id": "s2",
                    "narration": "أحيانًا نربط البداية بالشعور المناسب فنؤجل الحركة نفسها.",
                },
                {
                    "id": "s3",
                    "narration": "عندها يصبح الطريق أوضح. اكتب كلمة واحدة على ورقة، ثم اخرج للمشي.",
                },
            ],
        }
        accepted = _validate_script_for_brief(value, plan, brief)
        self.assertEqual(
            accepted["sections"][2]["narration"],
            "عندها يصبح الطريق أوضح. اكتب كلمة واحدة على ورقة.",
        )
        validate_short_script(accepted)

    def test_mistral_only_gets_explicit_short_contract_preflight(self) -> None:
        brief = _TEMPLATE_FIXTURES["inner_dialogue"]["brief"]
        plan = _plan(_TEMPLATE_FIXTURES["inner_dialogue"]["queries"])
        valid = {
            "title": "شورت",
            "sections": [
                {"id": "s1", "narration": "قد يختفي الدافع حين تنتظر الشعور قبل أن تبدأ."},
                {"id": "s2", "narration": "أحيانًا نربط البداية بالشعور المناسب فنؤجل الحركة نفسها."},
                {"id": "s3", "narration": "ابدأ بخطوة صغيرة تستطيع تنفيذها الآن."},
            ],
        }
        seen = {}

        def fail(name):
            def call(prompt, _tokens):
                seen[name] = prompt
                raise RuntimeError(name + "_capacity")
            return call

        def mistral(prompt, _tokens):
            seen["mistral"] = prompt
            return valid

        router = ProviderRouter(
            (
                ProviderAdapter("gemini", fail("gemini")),
                ProviderAdapter("groq", fail("groq")),
                ProviderAdapter("openrouter", fail("openrouter")),
                ProviderAdapter("mistral", mistral),
            )
        )
        base_prompt = _script_prompt(brief, plan)
        accepted = router.route(
            stage="script",
            prompt=base_prompt,
            max_tokens=400,
            validator=lambda value: _validate_script_for_brief(value, plan, brief),
        )

        self.assertEqual(accepted["sections"][0]["narration"], valid["sections"][0]["narration"])
        for provider in ("gemini", "groq", "openrouter"):
            self.assertEqual(seen[provider], base_prompt)
            self.assertNotIn("MISTRAL_SHORT_HOOK_COMPLIANCE", seen[provider])
            self.assertNotIn("MISTRAL_SHORT_S3_COMPLIANCE", seen[provider])
        self.assertIn("MISTRAL_SHORT_HOOK_COMPLIANCE", seen["mistral"])
        self.assertIn("preferably 8-16 words", seen["mistral"])
        self.assertIn("split the first sentence on whitespace", seen["mistral"])
        self.assertIn("NEVER more than 18", seen["mistral"])
        self.assertIn("if count > 18", seen["mistral"])
        self.assertIn("without fragmenting the sentence", seen["mistral"])
        self.assertIn("MISTRAL_SHORT_S3_COMPLIANCE", seen["mistral"])
        self.assertIn("Exactly ONE s3 sentence", seen["mistral"])
        self.assertIn("count action sentences", seen["mistral"])
        self.assertIn("require exactly 1", seen["mistral"])
        self.assertIn("scan every payoff sentence", seen["mistral"])

    def test_provider_router_rejects_technically_successful_hook_over_18_words(self) -> None:
        brief = _TEMPLATE_FIXTURES["inner_dialogue"]["brief"]
        plan = _plan(_TEMPLATE_FIXTURES["inner_dialogue"]["queries"])
        overlong = {
            "title": "شورت",
            "sections": [
                {
                    "id": "s1",
                    "narration": "هذا هوك طويل جدًا لأنه يشرح الفكرة بتفاصيل كثيرة لا نحتاجها الآن ويواصل الكلام حتى يتجاوز الحد الصلب بوضوح.",
                },
                {
                    "id": "s2",
                    "narration": "لاحظ اللحظة التي تنتظر فيها الشعور قبل أن تتحرك، دون لوم أو مبالغة.",
                },
                {
                    "id": "s3",
                    "narration": "اختر خطوة صغيرة تستطيع تنفيذها الآن.",
                },
            ],
        }
        valid = {
            "title": "شورت",
            "sections": [
                {
                    "id": "s1",
                    "narration": "قد يختفي الدافع حين تنتظر الشعور قبل أن تبدأ.",
                },
                {
                    "id": "s2",
                    "narration": "لاحظ اللحظة التي تنتظر فيها الشعور قبل أن تتحرك، دون لوم أو مبالغة.",
                },
                {
                    "id": "s3",
                    "narration": "اختر خطوة صغيرة تستطيع تنفيذها الآن.",
                },
            ],
        }

        router = ProviderRouter(
            (
                ProviderAdapter("groq", lambda _prompt, _tokens: overlong),
                ProviderAdapter("mistral", lambda _prompt, _tokens: valid),
            )
        )
        accepted = router.route(
            stage="script",
            prompt="short-script-contract-probe",
            max_tokens=400,
            validator=lambda value: _validate_script_for_brief(value, plan, brief),
        )

        self.assertEqual(accepted["sections"][0]["narration"], valid["sections"][0]["narration"])
        self.assertEqual(
            [(event["provider"], event["result"]) for event in router.events],
            [("groq", "invalid_output"), ("mistral", "success")],
        )
        self.assertIn("shortformaterror", str(router.events[0]["reason"]))
        self.assertEqual(SHORT_HOOK_MAX_WORDS, 18)
        with self.assertRaisesRegex(ShortFormatError, "short_hook_too_long"):
            validate_short_hook_contract(overlong)

    def test_small_hook_overrun_trims_only_at_safe_boundary(self) -> None:
        brief = _TEMPLATE_FIXTURES["inner_dialogue"]["brief"]
        plan = _plan(_TEMPLATE_FIXTURES["inner_dialogue"]["queries"])
        value = {
            "title": "شورت",
            "sections": [
                {
                    "id": "s1",
                    "narration": "قد تفقد الدافع حين تنتظر الشعور المناسب قبل أن تبدأ يومك رغم أنك تعرف المطلوب، لكن خطوة صغيرة تكفي.",
                },
                {
                    "id": "s2",
                    "narration": "لاحظ اللحظة التي تنتظر فيها الشعور قبل أن تتحرك دون لوم أو مبالغة.",
                },
                {
                    "id": "s3",
                    "narration": "اختر خطوة صغيرة تستطيع تنفيذها الآن.",
                },
            ],
        }

        accepted = _validate_script_for_brief(value, plan, brief)

        self.assertEqual(
            accepted["sections"][0]["narration"],
            "قد تفقد الدافع حين تنتظر الشعور المناسب قبل أن تبدأ يومك رغم أنك تعرف المطلوب.",
        )
        report = validate_short_hook_contract(accepted)
        self.assertLessEqual(report["hook_words"], SHORT_HOOK_MAX_WORDS)

    def test_small_hook_overrun_without_safe_boundary_still_fails_closed(self) -> None:
        brief = _TEMPLATE_FIXTURES["inner_dialogue"]["brief"]
        plan = _plan(_TEMPLATE_FIXTURES["inner_dialogue"]["queries"])
        value = {
            "title": "شورت",
            "sections": [
                {
                    "id": "s1",
                    "narration": "قد تفقد الدافع حين تنتظر الشعور المناسب قبل أن تبدأ يومك وتستمر في التأجيل دون فهم واضح لما يمنعك من الحركة الآن.",
                },
                {
                    "id": "s2",
                    "narration": "لاحظ اللحظة التي تنتظر فيها الشعور قبل أن تتحرك دون لوم أو مبالغة.",
                },
                {
                    "id": "s3",
                    "narration": "اختر خطوة صغيرة تستطيع تنفيذها الآن.",
                },
            ],
        }

        with self.assertRaisesRegex(
            ShortFormatError,
            r"short_hook_too_long words=22 maximum=18",
        ):
            _validate_script_for_brief(value, plan, brief)

        self.assertEqual(
            value["sections"][0]["narration"],
            "قد تفقد الدافع حين تنتظر الشعور المناسب قبل أن تبدأ يومك وتستمر في التأجيل دون فهم واضح لما يمنعك من الحركة الآن.",
        )

    def test_inner_dialogue_hook_visual_can_use_active_pressure_not_only_calm_reflection(self) -> None:
        brief = _TEMPLATE_FIXTURES["inner_dialogue"]["brief"]
        plan = _plan(
            [
                "tense hands stopping mid action unfinished task pressure",
                "reflective person alone pausing in quiet room",
                "contemplative person calmly closing notebook and standing",
            ]
        )
        report = validate_short_visual_queries(plan, brief)
        self.assertEqual(report["status"], "pass")
        prompt = short_prompt_context(brief)
        self.assertIn("do NOT make the hook visually calm", prompt)
        self.assertIn("scroll-stop visual beat", prompt)
        self.assertIn("strong answer to the hook", prompt)

    def test_inner_dialogue_rejects_middle_to_payoff_action_repeat(self) -> None:
        brief = _TEMPLATE_FIXTURES["inner_dialogue"]["brief"]
        plan = _plan(
            [
                "thoughtful person alone walking slowly in quiet room",
                "reflective person alone writing in notebook at desk",
                "quiet contemplative person writing on paper at desk",
            ]
        )
        with self.assertRaisesRegex(
            ShortFormatError,
            "short_visual_query_inner_dialogue_payoff_repeats_middle_action",
        ):
            validate_short_visual_queries(plan, brief)

    def test_short_color_cohesion_rejects_near_monochrome_candidate_before_grade(self) -> None:
        self.assertEqual(SHORT_MIN_COLOR_SATURATION_AVG, 5.0)
        source = StockVisualSource()
        plan = {
            "sections": [
                {"id": "s1", "visual_query_en": "quiet reflective person by window"},
            ]
        }
        pexels = {
            "provider": "pexels",
            "asset_id": "mono",
            "download_url": "https://videos.pexels.com/mono.mp4",
            "source_url": "https://pexels.com/mono",
            "creator": "fixture",
            "creator_url": "https://pexels.com",
            "query": "quiet reflective person by window",
        }
        pixabay = {
            "provider": "pixabay",
            "asset_id": "color",
            "download_url": "https://cdn.pixabay.com/color.mp4",
            "source_url": "https://pixabay.com/color",
            "creator": "fixture",
            "creator_url": "",
            "query": "quiet reflective person by window",
        }
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            source, "_pexels", return_value=pexels
        ), mock.patch.object(
            source, "_pixabay", return_value=pixabay
        ), mock.patch(
            "clean_v2.media._download_media",
            side_effect=lambda _url, destination: Path(destination).write_bytes(b"V" * 2048),
        ), mock.patch(
            "clean_v2.media._short_visual_color_compatible",
            side_effect=[
                (False, "short_near_monochrome saturation_avg=0.50"),
                (True, None),
            ],
        ):
            clips, rights = source.acquire(
                plan,
                Path(temporary),
                "short",
                5,
                section_estimated_seconds={"s1": 5.0},
            )

        self.assertEqual(len(clips), 1)
        self.assertEqual(rights[0]["provider"], "pixabay")
        rejected = [
            event for event in source.events
            if event.get("result") == "color_rejected"
        ]
        self.assertEqual(len(rejected), 1)
        self.assertIn("short_near_monochrome", str(rejected[0]["reason"]))

    def test_short_visual_lite_keeps_provider_assets_fixed_and_expands_edit_beats_locally(self) -> None:
        plan = _plan(
            [
                "thoughtful person alone pausing by window",
                "reflective person quietly closing phone",
                "contemplative person calmly taking one step",
            ]
        )
        source = StockVisualSource()
        counter = {"value": 0}

        def candidate(_query, *, portrait):
            counter["value"] += 1
            return {
                "provider": "pexels",
                "asset_id": str(counter["value"]),
                "download_url": f"https://videos.pexels.com/video-{counter['value']}.mp4",
                "source_url": "https://pexels.com",
                "creator": "fixture",
                "creator_url": "https://pexels.com",
                "query": _query,
            }

        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            source, "_pexels", side_effect=candidate
        ), mock.patch.object(
            source, "_pixabay", return_value=None
        ), mock.patch(
            "clean_v2.media._download_media",
            side_effect=lambda _url, destination: Path(destination).write_bytes(b"V" * 2048),
        ):
            clips, rights = source.acquire(
                plan,
                Path(temporary),
                "short",
                9,
                section_estimated_seconds={"s1": 12.0, "s2": 12.0, "s3": 12.0},
            )

        self.assertEqual(len(clips), SHORT_STOCK_ASSET_MAX)
        self.assertEqual(len(rights), SHORT_STOCK_ASSET_MAX)
        self.assertEqual(
            [row["section_id"] for row in rights],
            ["s1", "s1", "s2", "s2", "s3", "s3"],
        )
        for index in range(0, 6, 2):
            self.assertNotEqual(rights[index]["query"], rights[index + 1]["query"])

        section_ids = ["s1", "s1", "s2", "s2", "s3", "s3"]
        self.assertEqual(
            len(_expand_short_visual_sequence(clips, section_ids, 28.0)),
            SHORT_VISUAL_MIN,
        )
        self.assertEqual(
            len(_expand_short_visual_sequence(clips, section_ids, 34.0)),
            SHORT_VISUAL_TARGET,
        )
        self.assertEqual(
            len(_expand_short_visual_sequence(clips, section_ids, 39.0)),
            8,
        )
        self.assertEqual(
            len(_expand_short_visual_sequence(clips, section_ids, 44.0)),
            SHORT_VISUAL_MAX,
        )
        self.assertLess(SHORT_CUT_DISSOLVE_SECONDS, 0.2)
        self.assertIn("saturation=0.90", SHORT_MASTER_LOOK_FILTER)
        self.assertIn("colorbalance=", SHORT_MASTER_LOOK_FILTER)
        self.assertIn("_short_motion_filter", inspect.getsource(media_module._trim_and_grade_clip))
        self.assertIn(
            "_expand_short_visual_sequence",
            inspect.getsource(media_module.render_video),
        )

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
    def test_timeline_first_render_pads_picture_to_measured_voice_duration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            visual = root / "visual.mp4"
            narration = root / "narration-mastered.wav"
            final = root / "final.mp4"

            subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=c=black:s=320x180:r=30:d=0.600",
                    "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(visual),
                ],
                check=True,
            )
            subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=48000:duration=1.127",
                    "-c:a", "pcm_s16le", str(narration),
                ],
                check=True,
            )
            (root / "timeline-first.json").write_text(
                json.dumps(
                    {
                        "status": "pass",
                        "timeline_owner": "measured_charon_voice",
                        "voice_seconds_measured": 1.127,
                    }
                ),
                encoding="utf-8",
            )

            seen: dict[str, float] = {}

            def compose(source, destination, *, fmt, timeline):
                del fmt, timeline
                seen["body_seconds"] = media_module.probe_duration(Path(source))
                shutil.copy2(source, destination)

            with mock.patch(
                "clean_v2.timeline_render.render_identity_composition",
                side_effect=compose,
            ):
                media_module.render_video(narration, [visual], final, "film")

            self.assertGreater(seen["body_seconds"], 1.08)
            self.assertLessEqual(abs(seen["body_seconds"] - 1.127), 0.04)
            self.assertLessEqual(
                abs(media_module.probe_duration(final) - 1.127),
                0.04,
            )

    def test_optional_local_ai_still_replaces_one_short_auxiliary_without_network_generation(self) -> None:
        plan = _plan(
            [
                "thoughtful person alone pausing by window",
                "reflective person quietly closing phone",
                "contemplative person calmly taking one step",
            ]
        )
        source = StockVisualSource()
        counter = {"value": 0}

        def candidate(_query, *, portrait):
            counter["value"] += 1
            return {
                "provider": "pexels",
                "asset_id": str(counter["value"]),
                "download_url": f"https://videos.pexels.com/video-{counter['value']}.mp4",
                "source_url": "https://pexels.com",
                "creator": "fixture",
                "creator_url": "https://pexels.com",
                "query": _query,
            }

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            still = root / "insert.png"
            still.write_bytes(b"I" * 4096)
            output = root / "visuals"

            def render_still(_source, destination):
                Path(destination).write_bytes(b"A" * 4096)
                return Path(destination)

            with mock.patch.dict(
                "os.environ",
                {"CLEAN_V2_SHORT_AI_STILL": str(still)},
                clear=False,
            ), mock.patch.object(
                source, "_pexels", side_effect=candidate
            ), mock.patch.object(
                source, "_pixabay", return_value=None
            ), mock.patch(
                "clean_v2.media._download_media",
                side_effect=lambda _url, destination: Path(destination).write_bytes(b"V" * 2048),
            ), mock.patch(
                "clean_v2.media._render_local_short_ai_still",
                side_effect=render_still,
            ):
                clips, rights = source.acquire(
                    plan,
                    output,
                    "short",
                    5,
                    section_estimated_seconds={"s1": 6.4, "s2": 6.3, "s3": 6.3},
                )

        self.assertEqual(len(clips), SHORT_VISUAL_MIN)
        local = [row for row in rights if row.get("provider") == "generated_local_ai_still"]
        self.assertEqual(len(local), 1)
        self.assertEqual(local[0]["section_id"], "s2")
        self.assertEqual(local[0]["generation_cost"], 0)
        self.assertEqual(local[0]["network_generation_calls"], 0)

    def test_duration_contract_has_no_editorial_target_only_operational_safety(self) -> None:
        self.assertEqual(SHORT_DURATION_SAFETY_MAX_SECONDS, 120.0)
        for seconds in (0.5, 34.0, 48.0, 57.0, 120.0):
            self.assertEqual(validate_short_duration(seconds, phase="test"), seconds)
        for seconds in (0.0, 120.001):
            with self.assertRaisesRegex(
                ShortFormatError, "short_duration_operational_safety_violation"
            ):
                validate_short_duration(seconds, phase="test")

        self.assertEqual(
            validate_short_dimensions(SHORT_WIDTH, SHORT_HEIGHT),
            (1080, 1920),
        )
        with self.assertRaisesRegex(ShortFormatError, "short_frame_mismatch"):
            validate_short_dimensions(1920, 1080)

    def test_contract_report_records_no_identity_no_opening_and_no_social_cta(self) -> None:
        report = short_contract_report(_TEMPLATE_FIXTURES["why_reframe"]["brief"])
        self.assertEqual(report["format"], "short")
        self.assertEqual(report["section_count"], 3)
        self.assertEqual(report["frame"], {"width": 1080, "height": 1920})
        self.assertEqual(report["social_cta"], "forbidden")
        self.assertEqual(report["narrative_identity"], "not_applicable")
        self.assertEqual(report["opening_director"], "not_applicable")


class ShortTimedTextTests(unittest.TestCase):
    def test_caption_lite_uses_one_large_arabic_font_one_yellow_word_and_no_slate(self) -> None:
        events = [
            {"start": 0.0, "end": 2.0, "text": "مرّ اليوم ولم أبدأ", "role": "hook"},
            {"start": 2.0, "end": 4.2, "text": "القائمة بدت أكبر مني", "role": "beat"},
            {"start": 4.2, "end": 6.5, "text": "ابدأ بمهمة واحدة الآن", "role": "payoff"},
        ]
        validated = validate_progressive_text(events)
        slate_index = choose_dark_slate_index(events, validated)
        ass = build_rich_ass(events, slate_index=slate_index)

        self.assertIsNone(slate_index)
        self.assertEqual(MAX_DARK_SLATES, 0)
        self.assertEqual(ACCENT_ASS, "&H0000D4FF")
        self.assertEqual(BODY_FONT, "Noto Sans Arabic")
        self.assertEqual(FOCUS_FONT, BODY_FONT)
        self.assertEqual(FOCUS_FONT_SIZE, BODY_FONT_SIZE)
        self.assertGreaterEqual(BODY_FONT_SIZE, 110)
        self.assertIn("Style: Caption", ass)
        self.assertNotIn("Slate", ass)
        self.assertNotIn("Style: Focus", ass)
        self.assertIn(r"{\c&H0000D4FF}", ass)
        self.assertIn(r"\fscx98\fscy98", ass)
        self.assertIn("\u202B", ass)
        self.assertGreater(ass.count("Dialogue:"), len(events))
        self.assertNotIn("drawbox", ass)

    def test_phrase_captions_stay_compact_and_preserve_voice_owned_section_edges(self) -> None:
        script = {
            "sections": [
                {"id": "s1", "narration": "مرّ اليوم ولم أبدأ رغم أن المهمة أمامي."},
                {"id": "s2", "narration": "القائمة بدت أكبر مني كلما نظرت إليها."},
                {"id": "s3", "narration": "ابدأ بمهمة واحدة صغيرة الآن."},
            ]
        }
        timeline = {
            "status": "pass",
            "section_events": [
                {"section_id": "s1", "start": 0.0, "end": 5.0},
                {"section_id": "s2", "start": 5.0, "end": 10.0},
                {"section_id": "s3", "start": 10.0, "end": 15.0},
            ],
        }
        events = build_events_from_voice_timeline(script=script, timeline_report=timeline)
        self.assertGreater(len(events), 3)
        self.assertEqual(events[0]["start"], 0.0)
        self.assertEqual(events[-1]["end"], 15.0)
        self.assertEqual(events[0]["role"], "hook")
        self.assertEqual(events[-1]["role"], "payoff")
        for event in events:
            words = len(str(event["text"]).split())
            self.assertLessEqual(words, CAPTION_MAX_WORDS)
            self.assertGreaterEqual(words, CAPTION_MIN_WORDS)

    def test_body_focus_split_preserves_authored_words(self) -> None:
        text = "لكن الحقيقة أن البداية الصغيرة تغيّر اتجاه اللحظة"
        body, focus = split_focus_phrase(text, "beat")
        self.assertEqual(" ".join((body + " " + focus).split()), text)


class ShortVoiceOwnedTimelineTests(unittest.TestCase):
    def test_voice_46_03_is_accepted_without_regeneration_or_extension(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            audio_dir = root / "audio"
            audio_dir.mkdir()
            narration = root / "narration-mastered.wav"
            narration.write_bytes(b"fixture")
            for index in range(1, 4):
                (audio_dir / f"{index:02d}.wav").write_bytes(b"section")
            durations = {
                "narration-mastered.wav": 46.03,
                "01.wav": 12.0,
                "02.wav": 14.0,
                "03.wav": 20.03,
            }
            with mock.patch(
                "clean_v2.timeline_first.probe_duration",
                side_effect=lambda path: durations[Path(path).name],
            ):
                report = build_short_voice_owned_timeline(
                    output_dir=root,
                    narration_path=narration,
                )
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["voice_seconds_measured"], 46.03)
            self.assertIsNone(report["editorial_target_seconds"])
            self.assertFalse(report["time_compression"])
            self.assertFalse(report["time_extension"])
            self.assertFalse(report["tts_regeneration_for_duration"])
            self.assertEqual(report["duration_repair_attempts"], 0)

        source = inspect.getsource(voice_timeline_module)
        self.assertNotIn("atempo=", source)
        self.assertNotIn("synthesize_", source)

    def test_measured_charon_voice_retimes_sections_and_timed_text_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            audio_dir = root / "audio"
            audio_dir.mkdir()
            narration = root / "narration-mastered.wav"
            narration.write_bytes(b"mastered")
            for index in range(1, 4):
                (audio_dir / f"{index:02d}.wav").write_bytes(b"section")

            durations = {
                "narration-mastered.wav": 36.0,
                "01.wav": 8.0,
                "02.wav": 10.0,
                "03.wav": 18.0,
            }

            with mock.patch(
                "clean_v2.timeline_first.probe_duration",
                side_effect=lambda path: durations[Path(path).name],
            ):
                report = build_short_voice_owned_timeline(
                    output_dir=root,
                    narration_path=narration,
                )

            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["timeline_owner"], "measured_charon_voice")
            self.assertFalse(report["time_compression"])
            self.assertFalse(report["tts_regeneration_for_duration"])
            self.assertEqual(
                section_duration_map(report),
                {"s1": 8.0, "s2": 10.0, "s3": 18.0},
            )
            self.assertEqual(
                [(item["start"], item["end"]) for item in report["section_events"]],
                [(0.0, 8.0), (8.0, 18.0), (18.0, 36.0)],
            )

            script = {
                "sections": [
                    {"id": "s1", "narration": "قد يختفي الدافع فجأة."},
                    {"id": "s2", "narration": "لكن البداية لا تحتاج انتظارًا طويلًا."},
                    {"id": "s3", "narration": "ابدأ بخطوة صغيرة الآن."},
                ]
            }
            events = build_events_from_voice_timeline(
                script=script,
                timeline_report=report,
            )
            self.assertEqual(events[0]["start"], 0.0)
            self.assertEqual(events[-1]["end"], report["voice_seconds_measured"])
            self.assertEqual(events[0]["role"], "hook")
            self.assertEqual(events[-1]["role"], "payoff")
            self.assertGreaterEqual(len(events), 4)
            for event in events:
                self.assertLessEqual(len(str(event["text"]).split()), CAPTION_MAX_WORDS)


class ShortAudioPolishTests(unittest.TestCase):
    def test_charon_corrective_mastering_reuses_certified_lite_profile_without_tempo_change(self) -> None:
        self.assertEqual(CHARON_CORRECTIVE_PROFILE, "audio-mastering-lite-charon-v1")
        for fragment in ("highpass=f=70", "equalizer=f=220", "equalizer=f=3200", "deesser=", "acompressor="):
            self.assertIn(fragment, CHARON_CORRECTIVE_FILTER)
        self.assertNotIn("atempo", CHARON_CORRECTIVE_FILTER)
        self.assertNotIn("rubberband", CHARON_CORRECTIVE_FILTER)

    def test_actual_db_levels_keep_music_and_sfx_below_mastered_narration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            narration = root / "narration-mastered.wav"
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=220:sample_rate=48000:duration=2.2",
                    "-af",
                    "volume=-10dB",
                    "-c:a",
                    "pcm_s16le",
                    str(narration),
                ],
                check=True,
            )
            narration_mean = _measure_mean_db(narration)
            self.assertGreater(SFX_TARGET_REL_DB, -24.0)
            self.assertLessEqual(SFX_TARGET_REL_DB, SFX_MAX_REL_DB)

            raw_music = root / "music-raw.wav"
            music = root / "music.wav"
            _generate_raw_music(raw_music, 2.2)
            music_source = inspect.getsource(_generate_raw_music)
            self.assertIn("anoisesrc", music_source)
            self.assertNotIn("sine=frequency", music_source)
            music_report = _normalize_relative(
                src=raw_music,
                dest=music,
                narration_mean_db=narration_mean,
                target_relative_db=MUSIC_TARGET_REL_DB,
                minimum_relative_db=MUSIC_MIN_REL_DB,
                maximum_relative_db=MUSIC_MAX_REL_DB,
            )
            self.assertGreaterEqual(
                music_report["relative_to_narration_db"], MUSIC_MIN_REL_DB
            )
            self.assertLessEqual(
                music_report["relative_to_narration_db"], MUSIC_MAX_REL_DB
            )

            raw_sfx = root / "sfx-raw.wav"
            sfx = root / "sfx.wav"
            _generate_raw_sfx(raw_sfx, frequency=523.25)
            sfx_report = _normalize_relative(
                src=raw_sfx,
                dest=sfx,
                narration_mean_db=narration_mean,
                target_relative_db=SFX_TARGET_REL_DB,
                minimum_relative_db=SFX_MIN_REL_DB,
                maximum_relative_db=SFX_MAX_REL_DB,
            )
            self.assertGreaterEqual(
                sfx_report["relative_to_narration_db"], SFX_MIN_REL_DB
            )
            self.assertLessEqual(
                sfx_report["relative_to_narration_db"], SFX_MAX_REL_DB
            )

    def test_generation_failure_is_fail_safe_not_production_block(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            final_path = root / "final.mp4"
            narration = root / "narration-mastered.wav"
            final_path.write_bytes(b"fixture")
            narration.write_bytes(b"fixture")

            with (
                mock.patch(
                    "clean_v2.short_audio_polish.probe_duration",
                    create=True,
                ),
                mock.patch(
                    "clean_v2.media.probe_duration",
                    return_value=12.0,
                ),
                mock.patch(
                    "clean_v2.short_audio_polish._measure_mean_db",
                    return_value=-18.0,
                ),
                mock.patch(
                    "clean_v2.short_audio_polish._generate_raw_music",
                    side_effect=RuntimeError("music unavailable"),
                ),
                mock.patch(
                    "clean_v2.short_audio_polish._generate_raw_sfx",
                    side_effect=RuntimeError("sfx unavailable"),
                ),
            ):
                report = apply_short_audio_polish(
                    output_dir=root,
                    final_path=final_path,
                    narration_path=narration,
                    timed_text_report=None,
                )

            self.assertEqual(report["status"], "skipped_fail_safe")
            self.assertTrue(report["fail_safe"])
            self.assertEqual(report["provider_calls_added"], 0)
            self.assertEqual(final_path.read_bytes(), b"fixture")


class ShortPipelineSeamTests(unittest.TestCase):
    def test_short_script_prompt_has_no_duration_target_and_reuses_template_context(self) -> None:
        fixture = _TEMPLATE_FIXTURES["inner_dialogue"]
        prompt = _script_prompt(fixture["brief"], _plan(fixture["queries"]))
        self.assertIn("50-80 authored Arabic words", prompt)
        self.assertIn("Do not write toward a target duration", prompt)
        self.assertIn("measured mastered voice owns the final runtime", prompt)
        self.assertNotIn("30-45 seconds", prompt)
        self.assertNotIn("34-38 second", prompt)
        self.assertIn("selected_template=inner_dialogue", prompt)
        self.assertIn("social CTA remains visual-only", prompt)
        self.assertIn("IDENTITY_SEQUENCE is also HOST-MANAGED", prompt)
        self.assertIn("وهنا في نداء اليقظة، نقترب من أفكار الحياة اليومية بوعيٍ أوضح.", prompt)
        self.assertIn("one continuous thought, not three separate announcements", prompt)
        self.assertIn("paradox, direct scene, real question", prompt)
        self.assertIn("resolve the SAME tension/question", prompt)
        self.assertIn("must not append a second action", prompt)
        self.assertIn("SPOKEN_NATURALNESS_LITE", prompt)
        self.assertIn("write for the ear, not the page", prompt)
        self.assertIn("complete miniature idea", prompt)
        self.assertIn("السبب الحقيقي", prompt)
        self.assertIn("ليس X بل Y", prompt)
        self.assertIn("مرّ اليوم ولم أبدأ", prompt)

    def test_short_sectioned_voice_passes_primary_only_without_changing_chunking(self) -> None:
        class FakeCharon:
            def __init__(self) -> None:
                self.last_provider = ""
                self.fallback_used = False
                self.charon_attempts = 0
                self.voice_roles = {"mode": "single_narrator", "narrator": "Charon"}
                self.voice_approval_status = "human_approved_reference"
                self.voice_reference_profile = "fixture"
                self.primary_only_flags: list[bool] = []

            def synthesize(self, text, path, *, primary_only=False):
                self.primary_only_flags.append(bool(primary_only))
                self.last_provider = "gemini:Charon"
                self.fallback_used = False
                self.charon_attempts = 1
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                Path(path).write_bytes(b"W" * 2048)
                return Path(path)

        sections = [
            {"id": "s1", "narration": "هذه جملة قصيرة للاختبار."},
            {"id": "s2", "narration": "هذه جملة ثانية قصيرة للاختبار."},
            {"id": "s3", "narration": "اختر خطوة واحدة واضحة الآن."},
        ]

        def fake_concat(_inputs, output):
            Path(output).write_bytes(b"C" * 4096)
            return Path(output)

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "narration.wav"
            voice = FakeCharon()
            with mock.patch("clean_v2.pipeline.concat_wav_parts", side_effect=fake_concat):
                report = _synthesize_sectioned_voice(
                    voice,
                    sections,
                    output,
                    require_charon_only=True,
                )

        self.assertEqual(report["voice_provider"], "gemini:Charon")
        self.assertFalse(report["voice_fallback_used"])
        self.assertTrue(voice.primary_only_flags)
        self.assertTrue(all(voice.primary_only_flags))

    def test_mid_run_charon_failure_restarts_whole_voice_with_nabra(self) -> None:
        class FakeNabra:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def synthesize(self, transcript, output_path):
                self.calls.append(str(transcript))
                Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                Path(output_path).write_bytes(b"N" * 2048)
                return Path(output_path)

        gemini_calls = {"count": 0}

        def fake_gemini(api_key, transcript, output_path, *, model, voice, style=""):
            del api_key, transcript, model, voice, style
            gemini_calls["count"] += 1
            if gemini_calls["count"] == 1:
                Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                Path(output_path).write_bytes(b"C" * 2048)
                return Path(output_path)
            raise RuntimeError("http 429")

        def fake_concat(_inputs, output):
            Path(output).write_bytes(b"J" * 4096)
            return Path(output)

        sections = [
            {"id": "s1", "narration": "هذه جملة أولى واضحة للاختبار."},
            {"id": "s2", "narration": "هذه جملة ثانية واضحة للاختبار."},
        ]

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            narration = root / "narration.wav"
            nabra = FakeNabra()
            synth = GeminiPrimaryNabraFallbackSynthesizer(
                "gemini-key",
                nabra=nabra,
            )
            with (
                mock.patch.object(
                    media_module,
                    "_legacy_voice_identity",
                    return_value=("Charon", "Orus"),
                ),
                mock.patch.object(
                    media_module,
                    "_assert_human_approved_voice_reference",
                    return_value="fixture",
                ),
                mock.patch.object(
                    media_module,
                    "_legacy_gemini_synthesize",
                    side_effect=fake_gemini,
                ),
                mock.patch.object(
                    media_module,
                    "_charon_retry_delay",
                    return_value=0,
                ),
                mock.patch(
                    "clean_v2.pipeline.concat_wav_parts",
                    side_effect=fake_concat,
                ),
            ):
                report = _synthesize_sectioned_voice(
                    synth,
                    sections,
                    narration,
                    require_charon_only=True,
                )

            persisted = json.loads(
                (root / "voice-sections.json").read_text(encoding="utf-8")
            )

        self.assertEqual(gemini_calls["count"], 4)
        self.assertEqual(len(nabra.calls), 2)
        self.assertEqual(report["voice_provider"], "nabra:af_msa")
        self.assertTrue(report["voice_fallback_used"])
        self.assertEqual(
            report["voice_restart_reason"],
            "charon_failed_after_route_lock",
        )
        self.assertEqual(report["charon_tts_attempts_before_restart"], 4)
        self.assertEqual(
            {section["provider"] for section in report["sections"]},
            {"nabra:af_msa"},
        )
        self.assertEqual(persisted["status"], "pass")
        self.assertEqual(persisted["voice_provider"], "nabra:af_msa")

    def test_short_charon_passes_viewer_facing_performance_direction_without_rewriting(self) -> None:
        captured: dict[str, object] = {}

        def fake_synthesize(api_key, transcript, output_path, *, model, voice, style=""):
            captured.update(
                {
                    "api_key": api_key,
                    "transcript": transcript,
                    "model": model,
                    "voice": voice,
                    "style": style,
                }
            )
            Path(output_path).write_bytes(b"W" * 2048)
            return Path(output_path)

        with tempfile.TemporaryDirectory() as temporary:
            synth = GeminiPrimaryPiperFallbackSynthesizer(
                "gemini-key",
                Path(temporary) / "unused.onnx",
                None,
            )
            transcript = "ابدأ بخطوة واحدة واضحة الآن."
            with (
                mock.patch.object(media_module, "_legacy_voice_identity", return_value=("Charon", "Orus")),
                mock.patch.object(media_module, "_assert_human_approved_voice_reference", return_value="fixture"),
                mock.patch.object(media_module, "_legacy_gemini_synthesize", side_effect=fake_synthesize),
            ):
                result = synth.synthesize(
                    transcript,
                    Path(temporary) / "out.wav",
                    primary_only=True,
                )

        # The TemporaryDirectory is intentionally gone here; assert the returned
        # destination identity, while the provider call itself already proved success
        # by requiring a >1 KiB output before synthesize() returned.
        self.assertEqual(result.name, "out.wav")
        self.assertEqual(captured["transcript"], transcript)
        self.assertEqual(captured["voice"], "Charon")
        self.assertEqual(captured["style"], SHORT_CHARON_STYLE)
        self.assertIn("immediately and conversationally", str(captured["style"]))
        self.assertIn("announcer-like", str(captured["style"]))

    def test_primary_only_charon_failure_never_calls_azure_or_piper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            model = Path(temporary) / "unused.onnx"
            synth = GeminiPrimaryPiperFallbackSynthesizer(
                "gemini-key",
                model,
                None,
                azure_api_key="azure-key",
                azure_region="eastus",
                azure_free_tier_confirmed=True,
                azure_voice_approved=True,
                allow_piper_fallback=True,
            )
            with (
                mock.patch.object(media_module, "_legacy_voice_identity", return_value=("Charon", "Orus")),
                mock.patch.object(media_module, "_assert_human_approved_voice_reference", return_value="fixture"),
                mock.patch.object(media_module, "_legacy_gemini_synthesize", side_effect=RuntimeError("http_503")),
                mock.patch.object(media_module, "_charon_retry_delay", return_value=None),
                mock.patch.object(synth.azure, "synthesize") as azure_call,
                mock.patch.object(synth.piper, "synthesize") as piper_call,
            ):
                with self.assertRaisesRegex(
                    VoiceInfrastructureError,
                    "primary_only_contract_no_fallback",
                ):
                    synth.synthesize(
                        "نص قصير",
                        Path(temporary) / "out.wav",
                        primary_only=True,
                    )
            azure_call.assert_not_called()
            piper_call.assert_not_called()

    def test_short_identity_is_fixed_locally_with_zero_provider_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            report = _short_identity_not_applicable(output)
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["provider_calls_added"], 0)
            persisted = json.loads(
                (output / "narrative-identity.json").read_text(encoding="utf-8")
            )
            self.assertEqual(persisted["transitions"], [])
            self.assertTrue(persisted["opener"])
            self.assertEqual(persisted["closer"], "")
            self.assertTrue(persisted["prayer_sentence"])

    def test_opening_director_short_is_local_not_applicable_before_any_provider_use(self) -> None:
        router = mock.Mock()
        visual_source = mock.Mock()
        with tempfile.TemporaryDirectory() as temporary:
            report = run_opening_director(
                output_dir=Path(temporary),
                plan={"sections": []},
                script={"sections": []},
                rights=[],
                fmt="short",
                narration_path=Path(temporary) / "missing.wav",
                visual_source=visual_source,
                router=router,
            )
        self.assertEqual(report["status"], "not_applicable")
        router.assert_not_called()
        visual_source.assert_not_called()

    def test_contextual_cta_binding_is_none_for_short(self) -> None:
        plan = SimpleNamespace(
            format="short",
            cta="اشترك في القناة",
            sections=[SimpleNamespace(id="s1", narration="نص تجريبي")],
        )
        binding = bind_contextual_cta(plan)
        self.assertEqual(binding.mode, CtaMode.NONE)
        self.assertEqual(binding.spoken_text, "")
        self.assertEqual(binding.reason, "short_no_cta")


class ShortNoFacePolicyTests(unittest.TestCase):
    def test_identifiable_person_is_deterministic_block(self) -> None:
        result = visual_qa_module._apply_no_face_policy(
            {
                "status": "pass",
                "identifiable_person": True,
                "relevance": 0.95,
                "visual_quality": 0.95,
                "reason": "otherwise acceptable",
            }
        )
        self.assertEqual(result["status"], "block")
        self.assertEqual(result["no_face_policy"], "block")
        self.assertIn("no_face_policy_identifiable_person", result["reason"])

    def test_no_identifiable_person_preserves_provider_verdict(self) -> None:
        result = visual_qa_module._apply_no_face_policy(
            {
                "status": "pass",
                "identifiable_person": False,
                "reason": "clean",
            }
        )
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["no_face_policy"], "pass")


if __name__ == "__main__":
    unittest.main()

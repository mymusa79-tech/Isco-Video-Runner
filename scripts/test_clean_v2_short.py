from __future__ import annotations

import inspect
import json
import shutil
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from clean_v2.contextual_cta import CtaMode, bind_contextual_cta
from clean_v2.identity_sequence import PRAYER_SENTENCE, SHORT_CHANNEL_DEFINITION
from clean_v2.contracts import ContractError, validate_plan
from clean_v2.opening_director import run_opening_director
from clean_v2.pipeline import (
    CleanV2Pipeline,
    _audit_narrative_format_for_brief,
    _lock_longform_narrative_format,
    _planning_prompt,
    _select_longform_narrative_profile,
    _script_prompt,
    _short_identity_not_applicable,
    _synthesize_sectioned_voice,
    _validate_script_for_brief,
)
from clean_v2 import media as media_module
from clean_v2 import ai_still as ai_still_module
from clean_v2 import visual_qa as visual_qa_module
from clean_v2.media import (
    GeminiOnlyVoiceSynthesizer,
    SHORT_CUT_DISSOLVE_SECONDS,
    SHORT_MIN_COLOR_SATURATION_AVG,
    StockVisualSource,
    VoiceInfrastructureError,
)
from clean_v2 import providers as providers_module
from clean_v2.providers import (
    ProviderAdapter,
    ProviderRouter,
    ProviderWireFailure,
    _provider_prompt,
    _safe_validator_reason,
)
from clean_v2.audio_mastering import GEMINI_CORRECTIVE_FILTER, GEMINI_CORRECTIVE_PROFILE
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
    PRIMARY_ASS,
    BODY_FONT,
    BODY_FONT_SIZE,
    CAPTION_MAX_WORDS,
    CAPTION_MIN_WORDS,
    COMPOSITION_MODE,
    SAFE_X_MAX,
    SAFE_X_MIN,
    SAFE_Y_MAX,
    SAFE_Y_MIN,
    FOCUS_FONT,
    FOCUS_FONT_SIZE,
    FOCUS_SCALE,
    MAX_DARK_SLATES,
    build_composition_hints,
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
from clean_v2.tone_audit import (
    _LEGACY_RELIGIOUS_QUOTE_RULE,
    _scope_clean_v2_tone_prompt,
)
from clean_v2.short_format import (
    SHORT_HEIGHT,
    SHORT_HOOK_MAX_WORDS,
    SHORT_HOOK_RESCUE_MAX_WORDS,
    SHORT_DURATION_SAFETY_MAX_SECONDS,
    SHORT_SECTION_COUNT,
    SHORT_WIDTH,
    ShortFormatError,
    TEMPLATE_VISUAL_QUERY_DIRECTIVES,
    apply_safe_short_s3_action_prefix_trim,
    apply_safe_short_s3_locked_payoff_fallback,
    apply_safe_short_s3_single_action_trim,
    materialize_short_s3,
    normalize_short_practical_action,
    normalize_short_script_candidate,
    normalize_short_visual_queries,
    select_short_template,
    short_contract_report,
    short_prompt_context,
    validate_short_dimensions,
    validate_short_duration,
    validate_short_hook_contract,
    validate_short_practical_action,
    validate_short_s3_contract,
    validate_short_script,
    validate_short_visual_queries,
    validate_short_visual_safety,
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
            "rushed worker tearing cluttered schedule beside missed deadline",
            "person pause reconsidering written plan at desk",
            "calm focused worker using simple practical schedule",
        ],
    },
    "inner_dialogue": {
        "brief": _brief("كيف تنهض عندما تفقد الدافع وتقول لنفسك لا أستطيع؟"),
        "queries": [
            "hesitating hands stopping over unfinished task under deadline pressure",
            "solitary person thinking in calm quiet room",
            "reflective person walking alone peaceful morning",
        ],
    },
    "micro_story": {
        "brief": _brief("قصة قصيرة: ذات يوم بدأت تجربة صغيرة ثم تغيرت النتيجة"),
        "queries": [
            "rushed woman opening notebook beside ringing deadline alarm",
            "woman writing notebook task list at desk",
            "woman closing notebook after finishing work",
        ],
    },
    "quote_reflection": {
        "brief": _brief("اقتباس للتأمل: «ابدأ بما تستطيع اليوم»"),
        "queries": [
            "reflective hand tearing failed note under stark quiet light",
            "calm person reading slowly in minimal room",
            "peaceful contemplative window scene with still light",
        ],
    },
}


class ShortMistralS3PromptClarityTests(unittest.TestCase):
    def test_mistral_script_prompt_respects_host_owned_s3_action(self) -> None:
        base = "SHORT_FORMAT_CONTRACT:\nbase contract"
        prompt = _provider_prompt(base, provider="mistral", stage="script")
        self.assertIn("LOCKED_PLAN.practical_action_ar is host-owned", prompt)
        self.assertIn("Do NOT write, repeat, paraphrase, or replace it", prompt)
        self.assertIn("ZERO practical-action/imperative markers", prompt)
        self.assertIn("purely descriptive state/result", prompt)
        self.assertNotIn("GOOD s3:", prompt)
        self.assertEqual(_provider_prompt(base, provider="groq", stage="script"), base)

        report = validate_short_script(
            {
                "sections": [
                    {
                        "id": "s1",
                        "narration": "تعرف ما تريد فعله، لكنك تبقى مكانك لأن البداية تبدو أثقل من المهمة.",
                    },
                    {
                        "id": "s2",
                        "narration": "السبب ليس غياب الرغبة دائمًا، بل أن المهمة الكبيرة ترفع الاحتكاك قبل أول خطوة.",
                    },
                    {
                        "id": "s3",
                        "narration": "المهمة الصغيرة تقلل الاحتكاك وتمنحك نقطة واضحة للعودة. اكتب مهمة واحدة تستطيع إنهاءها الآن.",
                    },
                ]
            }
        )
        self.assertEqual(report["practical_action_sentences"], 1)
        self.assertEqual(report["practical_action_markers"], 1)


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

    def test_longform_host_owned_narrative_format_is_locked_before_validation(self) -> None:
        for fmt in ("film", "podcast"):
            with self.subTest(format=fmt):
                brief = _brief("كيف تستعيد تركيزك بعد أيام من التشتت؟")
                brief["format"] = fmt
                raw = {"narrative_format": "provider-typo-profile"}
                locked = _lock_longform_narrative_format(raw, brief)
                self.assertEqual(
                    locked["narrative_format"],
                    _select_longform_narrative_profile(brief)["narrative_format"],
                )
                self.assertEqual(raw["narrative_format"], "provider-typo-profile")
        podcast = _brief("لماذا نؤجل ما نعرف أنه مهم؟")
        podcast["format"] = "podcast"
        self.assertEqual(
            _lock_longform_narrative_format({}, podcast)["narrative_format"],
            "dialogue_qa",
        )

    def test_editorial_dependency_and_visual_evidence_are_shared_without_new_schema(self) -> None:
        story = {
            "retention_thread": {
                "hook_tension": "توتر محدد",
                "payoff_answer": "نتيجة مستحقة",
                "visual_motif": "حالة تتغير",
            },
            "beats": [],
        }
        for fmt in ("short", "film", "podcast"):
            with self.subTest(format=fmt):
                brief = _brief("لماذا تجعلنا كثرة الخيارات أقل حسمًا؟")
                brief["format"] = fmt
                planning = _planning_prompt(brief)
                self.assertIn("EDITORIAL DEPENDENCY CONTRACT", planning)
                self.assertIn("Section order must matter", planning)
                self.assertIn("VISUAL EVIDENCE CONTRACT", planning)
                self.assertIn("What can the viewer literally see here", planning)
                self.assertIn("desk/laptop/notebook/writing", planning)

                script = _script_prompt(
                    brief,
                    _plan(_TEMPLATE_FIXTURES["why_reframe"]["queries"]),
                    visual_story=story,
                )
                self.assertIn("EDITORIAL DEPENDENCY CONTRACT", script)
                self.assertIn("two adjacent", script)
                self.assertIn("interchangeable", script)

    def test_podcast_listener_proxy_turns_must_unlock_new_information(self) -> None:
        podcast = _brief("لماذا نعرف ما يجب فعله ثم نؤجله؟")
        podcast["format"] = "podcast"
        planning = _planning_prompt(podcast)
        self.assertIn("real question, a plausible doubt, a concrete objection, or a request for clarification", planning)
        self.assertIn("If B would deliver", planning)
        self.assertIn("essentially the same substance without that A turn, omit A", planning)
        self.assertIn("do not invent a new schema or metadata field", planning)

        script = _script_prompt(
            podcast,
            _plan(_TEMPLATE_FIXTURES["why_reframe"]["queries"]),
            visual_story={
                "retention_thread": {
                    "hook_tension": "التأجيل رغم معرفة المطلوب",
                    "payoff_answer": "تمييز يفسر الفجوة",
                    "visual_motif": "مهمة معلقة ثم محسومة",
                },
                "beats": [],
            },
        )
        self.assertIn("Every A turn must perform exactly one useful listener-proxy job", script)
        self.assertIn("B must answer the specific gap opened by A", script)
        self.assertIn("If removing an A turn would leave B", script)
        self.assertIn("saying essentially the same thing", script)

        short = _brief("لماذا نؤجل ما نعرف أنه مهم؟")
        self.assertNotIn(
            "Every A turn must perform exactly one useful listener-proxy job",
            _script_prompt(
                short,
                _plan(_TEMPLATE_FIXTURES["why_reframe"]["queries"]),
                visual_story={
                    "retention_thread": {
                        "hook_tension": "تأجيل واضح",
                        "payoff_answer": "تفسير واضح",
                        "visual_motif": "مهمة تتغير حالتها",
                    },
                    "beats": [],
                },
            ),
        )

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
                "tense hands gripping unfinished notebook under deadline pressure at wooden table",
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
            "short_visual_query_hook_requires_immediate_tension",
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
                self.assertIn("practical_action_ar", prompt)
                self.assertIn("hands only", prompt)
                self.assertIn("dominant action family", prompt)
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



class ShortImmediateTensionContractTests(unittest.TestCase):
    def test_generic_calm_hook_is_rejected_locally(self) -> None:
        with self.assertRaisesRegex(
            ShortFormatError,
            "short_hook_generic_calm_opening",
        ):
            validate_short_hook_contract({
                "sections": [
                    {
                        "id": "s1",
                        "narration": "في حياتنا نمر أحيانًا بأيام نشعر فيها أن الأمور ليست واضحة.",
                    }
                ]
            })

    def test_direct_question_and_explicit_contrast_hooks_pass(self) -> None:
        question = validate_short_hook_contract({
            "sections": [
                {
                    "id": "s1",
                    "narration": "لماذا تفقد طاقتك قبل أن ينتهي يوم العمل؟",
                }
            ]
        })
        self.assertEqual(question["immediate_tension_shape"], "direct_question")

        contrast = validate_short_hook_contract({
            "sections": [
                {
                    "id": "s1",
                    "narration": "تعرف المهمة جيدًا، لكنك تبقى مكانك عندما يحين وقت البدء.",
                }
            ]
        })
        self.assertEqual(contrast["immediate_tension_shape"], "explicit_contrast")

    def test_quiet_generic_first_visual_is_rejected_and_tense_action_passes(self) -> None:
        brief = _TEMPLATE_FIXTURES["inner_dialogue"]["brief"]
        quiet = _plan([
            "thoughtful person sitting in quiet room by window",
            "solitary person thinking in calm quiet room",
            "reflective person walking alone peaceful morning",
        ])
        with self.assertRaisesRegex(
            ShortFormatError,
            "short_visual_query_hook_calm_or_generic",
        ):
            validate_short_visual_queries(quiet, brief)

        active = _plan([
            "tense hands stopping mid action over unfinished task under deadline pressure",
            "solitary person thinking in calm quiet room",
            "reflective person walking alone peaceful morning",
        ])
        report = validate_short_visual_queries(active, brief)
        self.assertEqual(report["status"], "pass")
        self.assertTrue(report["hook_visual"]["action_hits"])
        self.assertTrue(report["hook_visual"]["tension_hits"])


class ShortHookBoundedRecoveryTests(unittest.TestCase):
    def test_four_word_overrun_trims_only_at_natural_boundary(self) -> None:
        script = {
            "sections": [
                {
                    "id": "s1",
                    "narration": (
                        "حين تفقد الدافع تمامًا لا يعني ذلك أنك كسول بل أن البداية تبدو ثقيلة، "
                        "لأنك تنتظر شعورًا كاملًا قبل أول خطوة صغيرة."
                    ),
                }
            ]
        }
        original_hook = script["sections"][0]["narration"]
        self.assertEqual(len(original_hook.split()), SHORT_HOOK_MAX_WORDS + 4)
        report = normalize_short_script_candidate(script)
        self.assertTrue(report["hook_trimmed"])
        accepted = validate_short_hook_contract(script)
        self.assertLessEqual(accepted["hook_words"], SHORT_HOOK_MAX_WORDS)
        self.assertEqual(
            accepted["hook"],
            "حين تفقد الدافع تمامًا لا يعني ذلك أنك كسول بل أن البداية تبدو ثقيلة.",
        )

    def test_long_hook_without_natural_boundary_stays_fail_closed(self) -> None:
        script = {
            "sections": [
                {
                    "id": "s1",
                    "narration": (
                        "حين تفقد الدافع تمامًا قد تظن أن المشكلة فيك لأن البداية ثقيلة ولأنك تنتظر "
                        "شعورًا كاملًا يساعدك على بدء أول خطوة صغيرة واضحة اليوم."
                    ),
                }
            ]
        }
        self.assertGreater(
            len(script["sections"][0]["narration"].split()),
            SHORT_HOOK_RESCUE_MAX_WORDS,
        )
        report = normalize_short_script_candidate(script)
        self.assertFalse(report["hook_trimmed"])
        with self.assertRaisesRegex(ShortFormatError, "short_hook_too_long"):
            validate_short_hook_contract(script)

    def test_run48_style_long_hook_splits_at_safe_boundary_without_losing_tail(self) -> None:
        script = {
            "sections": [
                {
                    "id": "s1",
                    "narration": (
                        "لماذا تظن أن كثرة المهام تعني أنك تحتاج خطة أقوى كل صباح، لكن المشكلة الحقيقية "
                        "أن يومك يبدأ أصلًا بأكثر مما تستطيع إنهاءه بهدوء ومن دون استنزاف؟"
                    ),
                }
            ]
        }
        original = script["sections"][0]["narration"]
        self.assertGreater(len(original.split()), SHORT_HOOK_RESCUE_MAX_WORDS)
        report = normalize_short_script_candidate(script)
        self.assertTrue(report["hook_trimmed"])
        accepted = validate_short_hook_contract(script)
        self.assertLessEqual(accepted["hook_words"], SHORT_HOOK_MAX_WORDS)
        self.assertIn("لكن المشكلة الحقيقية", script["sections"][0]["narration"])


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


    def test_terminal_local_patch_rejection_does_not_try_next_provider(self) -> None:
        calls: list[str] = []

        def first(_prompt: str, _max_tokens: int) -> dict:
            calls.append("first")
            return {"patches": [{"section_id": "s3", "find": "x", "replace": "y"}]}

        def second(_prompt: str, _max_tokens: int) -> dict:
            calls.append("second")
            return {"patches": [{"section_id": "s3", "find": "x", "replace": "y"}]}

        class TerminalLocalRejection(ValueError):
            terminal_provider_fallback = True

        router = ProviderRouter(
            [
                ProviderAdapter("first", first),
                ProviderAdapter("second", second),
            ]
        )
        with self.assertRaises(TerminalLocalRejection):
            router.route(
                stage="script_patch",
                prompt="bounded patch",
                max_tokens=32,
                validator=lambda _value: (_ for _ in ()).throw(
                    TerminalLocalRejection("locked_action")
                ),
            )
        self.assertEqual(calls, ["first"])


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
                {"id": "s1", "narration": "لماذا أتوقف رغم أنني أريد أن أبدأ؟ حين أهدأ قليلًا أرى ما يحدث بوضوح."},
                {"id": "s2", "narration": "الفكرة الصغيرة هنا أن تلاحظ اللحظة التي تنسحب فيها من الفعل، دون لوم أو مبالغة."},
                {"id": "s3", "narration": "اختر حركة بسيطة تستطيع تنفيذها الآن؛ فالخطوة التالية تتضح بعد البداية."},
            ],
        }
        report = validate_short_script(valid)
        self.assertTrue(report["single_voice"])
        self.assertFalse(report["social_cta"])
        self.assertLessEqual(report["hook_words"], SHORT_HOOK_MAX_WORDS)

        dialogue = json.loads(json.dumps(valid, ensure_ascii=False))
        dialogue["sections"][0]["narration"] = "A: لم أبدأ رغم أن الوقت يمر. B: هل أبدأ الآن؟"
        with self.assertRaisesRegex(ShortFormatError, "single_voice"):
            validate_short_script(dialogue)

        cta = json.loads(json.dumps(valid, ensure_ascii=False))
        cta["sections"][2]["narration"] += " اشترك في القناة."
        with self.assertRaisesRegex(ShortFormatError, "zero_social_cta"):
            validate_short_script(cta)

    def test_run63_structured_s3_separates_payoff_from_locked_action(self) -> None:
        payoff = (
            "مسارك الزمني خاص بك، وقيمتك لا تُقاس بسرعة شخص آخر أو ترتيب ظهوره أمامك."
        )
        action = "اكتب هدفًا شخصيًا واحدًا اليوم."
        script = {
            "title": "لماذا تفشل المقارنة في قياس سعادتك؟",
            "sections": [
                {
                    "id": "s1",
                    "narration": "هل تقارن إنجازاتك اليومية بمسارات الآخرين وتفترض أنك متأخر عنهم في سباق غير موجود؟",
                },
                {
                    "id": "s2",
                    "narration": "المقارنة تنقل معيارك من تقدمك الفعلي إلى صورة شخص آخر، فيضيع قياسك الحقيقي.",
                },
                {
                    "id": "s3",
                    "narration": f"{payoff} {action}",
                    "s3_payoff": payoff,
                    "s3_locked_action": action,
                },
            ],
        }

        # The final spoken narration contains the action-family verb «اكتب», but
        # payoff validation receives only s3_payoff and action validation receives
        # only s3_locked_action. The old circular merged-string failure is gone.
        report = validate_short_script(script)
        self.assertEqual(report["practical_action_sentences"], 1)
        self.assertEqual(report["practical_action_markers"], 1)
        s3 = validate_short_s3_contract(payoff, action)
        self.assertEqual(s3["s3_payoff"], payoff)
        self.assertEqual(s3["s3_locked_action"], action)

    def test_run63_writer_contract_materializes_action_only_after_validation(self) -> None:
        brief = _TEMPLATE_FIXTURES["inner_dialogue"]["brief"]
        plan = _plan(_TEMPLATE_FIXTURES["inner_dialogue"]["queries"])
        plan["practical_action_ar"] = "اكتب هدفًا شخصيًا واحدًا اليوم."
        candidate = {
            "title": "شورت",
            "sections": [
                {
                    "id": "s1",
                    "narration": "هل تقارن يومك بمسار شخص آخر ثم تعتبر نفسك متأخرًا رغم اختلاف الطريق؟",
                },
                {
                    "id": "s2",
                    "narration": "حين يتغير معيار القياس كل مرة، يبدو تقدمك أصغر حتى لو كان حقيقيًا وواضحًا.",
                },
                {
                    "id": "s3",
                    "s3_payoff": "المعيار الأصدق هو تقدمك أنت مقارنة بنقطة بدايتك وهدفك الحالي.",
                },
            ],
        }

        normalized = _validate_script_for_brief(candidate, plan, brief)
        closing = normalized["sections"][2]
        self.assertEqual(
            closing["s3_locked_action"],
            plan["practical_action_ar"],
        )
        self.assertEqual(
            closing["narration"],
            f"{closing['s3_payoff']} {closing['s3_locked_action']}",
        )
        validate_short_script(normalized)

    def test_cohort_attempt_2_s3_requires_one_direct_practical_action(self) -> None:
        prompt = short_prompt_context(_TEMPLATE_FIXTURES["inner_dialogue"]["brief"])
        self.assertIn("practical_action_ar MUST begin with a direct Arabic imperative verb", prompt)
        self.assertIn("Planning self-check", prompt)
        self.assertIn("Script self-check", prompt)
        self.assertIn("s3_payoff", prompt)
        self.assertIn("s3_locked_action", prompt)

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

        attached_object_action = json.loads(json.dumps(no_action, ensure_ascii=False))
        attached_object_action["sections"][2]["narration"] = (
            "عندها يصبح الطريق أوضح. اكتبها."
        )
        report = validate_short_script(attached_object_action)
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

        attached_object = json.loads(json.dumps(base, ensure_ascii=False))
        attached_object["sections"][2]["narration"] = (
            "لا تحتاج إلى كل القائمة. يكفي كلمة واحدة فقط، الآن. اكتبها."
        )
        original = attached_object["sections"][2]["narration"]
        self.assertFalse(apply_safe_short_s3_single_action_trim(attached_object))
        self.assertEqual(attached_object["sections"][2]["narration"], original)
        report = validate_short_script(attached_object)
        self.assertEqual(report["practical_action_sentences"], 1)
        self.assertEqual(report["practical_action_markers"], 1)

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

        clause_salvage = json.loads(json.dumps(base, ensure_ascii=False))
        clause_salvage["sections"][2]["narration"] = (
            "حين تبدأ بخطوة صغيرة، يعود الإحساس بالقدرة بعد أول نتيجة. "
            "اختر مهمة واحدة الآن."
        )
        self.assertTrue(apply_safe_short_s3_single_action_trim(clause_salvage))
        self.assertEqual(
            clause_salvage["sections"][2]["narration"],
            "يعود الإحساس بالقدرة بعد أول نتيجة. اختر مهمة واحدة الآن.",
        )
        validate_short_script(clause_salvage)

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

    def test_locked_planning_payoff_repairs_only_all_forbidden_payoff_prose(self) -> None:
        script = {
            "title": "شورت",
            "sections": [
                {"id": "s1", "narration": "قد تتعطل خطتك حين تبدو البداية أكبر من طاقتك."},
                {"id": "s2", "narration": "تصغير الاحتكاك يجعل الاستمرار أقرب وأوضح."},
                {
                    "id": "s3",
                    "narration": "البداية الصغيرة تكسر الجمود. اختر مهمة واحدة الآن.",
                },
            ],
        }
        self.assertTrue(
            apply_safe_short_s3_locked_payoff_fallback(
                script,
                "المهمة الأصغر تقلل الاحتكاك وتعيد الإحساس بالقدرة.",
            )
        )
        self.assertEqual(
            script["sections"][2]["narration"],
            "المهمة الأصغر تقلل الاحتكاك وتعيد الإحساس بالقدرة. اختر مهمة واحدة الآن.",
        )
        validate_short_script(script)

        unsafe = json.loads(json.dumps(script, ensure_ascii=False))
        unsafe["sections"][2]["narration"] = (
            "البداية الصغيرة تكسر الجمود. اختر مهمة واحدة الآن."
        )
        self.assertFalse(
            apply_safe_short_s3_locked_payoff_fallback(
                unsafe,
                "ابدأ بخطوة أصغر وستشعر بالقدرة.",
            )
        )

    def test_artifact_149_locked_payoff_uses_local_last_resort(self) -> None:
        script = {
            "title": "شورت",
            "sections": [
                {"id": "s1", "narration": "قد يختفي الدافع حين تنتظر الشعور قبل أن تبدأ."},
                {"id": "s2", "narration": "الاحتكاك العالي يجعل المهمة أثقل من حجمها الحقيقي."},
                {
                    "id": "s3",
                    "narration": "البداية الصغيرة تكسر الجمود. اختر مهمة واحدة الآن.",
                },
            ],
        }
        locked_payoff = (
            "يظهر أثر البداية الجديدة، مع شعور بالتحول من الجمود إلى الحركة."
        )

        original = script["sections"][2]["narration"]
        self.assertFalse(
            apply_safe_short_s3_locked_payoff_fallback(script, locked_payoff)
        )
        self.assertEqual(script["sections"][2]["narration"], original)
        with self.assertRaisesRegex(
            ShortFormatError,
            "short_s3_payoff_contains_forbidden_action_family",
        ):
            validate_short_script(script)

    def test_artifact_149_provider_outage_family_accepts_mistral_s3_via_local_rescue(self) -> None:
        brief = _TEMPLATE_FIXTURES["inner_dialogue"]["brief"]
        plan = _plan(_TEMPLATE_FIXTURES["inner_dialogue"]["queries"])
        candidate = {
            "title": "شورت",
            "sections": [
                {"id": "s1", "narration": "قد يختفي الدافع حين تنتظر الشعور قبل أن تبدأ."},
                {"id": "s2", "narration": "الاحتكاك العالي يجعل المهمة أثقل من حجمها الحقيقي."},
                {
                    "id": "s3",
                    "narration": "البداية الصغيرة تكسر الجمود. اختر مهمة واحدة الآن.",
                },
            ],
        }
        visual_story = {
            "retention_thread": {
                "payoff_answer": (
                    "المهمة الأصغر تقلل الاحتكاك وتعيد الإحساس بالقدرة."
                )
            }
        }

        def fail(reason: str, status: int):
            def call(_prompt, _tokens):
                raise ProviderWireFailure(reason, http_status=status)
            return call

        router = ProviderRouter(
            (
                ProviderAdapter("gemini", fail("http_503", 503)),
                ProviderAdapter("groq", fail("http_429", 429)),
                ProviderAdapter("openrouter", lambda _prompt, _tokens: candidate),
                ProviderAdapter("mistral", lambda _prompt, _tokens: candidate),
            )
        )
        router._rate_limited_for_run.add("openrouter")
        with mock.patch.object(providers_module.time, "sleep"):
            accepted = router.route(
                stage="script",
                prompt=_script_prompt(brief, plan, visual_story=visual_story),
                max_tokens=400,
                validator=lambda value: _validate_script_for_brief(
                    value,
                    plan,
                    brief,
                    visual_story,
                ),
            )

        self.assertEqual(
            accepted["sections"][2]["narration"],
            "المهمة الأصغر تقلل الاحتكاك وتعيد الإحساس بالقدرة. اختر مهمة واحدة الآن.",
        )
        validate_short_script(accepted)
        self.assertEqual(
            [(event["provider"], event["result"]) for event in router.events],
            [
                ("gemini", "retrying"),
                ("gemini", "failed"),
                ("groq", "failed"),
                ("openrouter", "unavailable"),
                ("mistral", "success"),
            ],
        )

    def test_pipeline_short_validator_uses_locked_visual_story_payoff_without_ai_retry(self) -> None:
        brief = _TEMPLATE_FIXTURES["inner_dialogue"]["brief"]
        plan = _plan(_TEMPLATE_FIXTURES["inner_dialogue"]["queries"])
        candidate = {
            "title": "شورت",
            "sections": [
                {"id": "s1", "narration": "قد يختفي الدافع حين تنتظر الشعور قبل أن تبدأ."},
                {"id": "s2", "narration": "الاحتكاك العالي يجعل المهمة أثقل من حجمها الحقيقي."},
                {
                    "id": "s3",
                    "narration": "البداية الصغيرة تكسر الجمود. اختر مهمة واحدة الآن.",
                },
            ],
        }
        visual_story = {
            "retention_thread": {
                "payoff_answer": "المهمة الأصغر تقلل الاحتكاك وتعيد الإحساس بالقدرة."
            }
        }
        accepted = _validate_script_for_brief(
            candidate,
            plan,
            brief,
            visual_story,
        )
        self.assertEqual(
            accepted["sections"][2]["narration"],
            "المهمة الأصغر تقلل الاحتكاك وتعيد الإحساس بالقدرة. اختر مهمة واحدة الآن.",
        )
        validate_short_script(accepted)

    def test_canonical_short_gate_trims_safe_discourse_prefix_before_action(self) -> None:
        script = {
            "title": "شورت",
            "sections": [
                {"id": "s1", "narration": "تتوقف أحيانًا لأن البداية تبدو أكبر من طاقتك."},
                {"id": "s2", "narration": "حين تصغر نقطة البدء يصبح الاحتكاك أقل."},
                {
                    "id": "s3",
                    "narration": "عندها يصبح الطريق أوضح. الآن ابدأ بمهمة واحدة تستطيع إنهاءها اليوم.",
                },
            ],
        }
        self.assertTrue(apply_safe_short_s3_action_prefix_trim(script))
        self.assertEqual(
            script["sections"][2]["narration"],
            "عندها يصبح الطريق أوضح. ابدأ بمهمة واحدة تستطيع إنهاءها اليوم.",
        )
        validate_short_script(script)

        unsafe = json.loads(json.dumps(script, ensure_ascii=False))
        unsafe["sections"][2]["narration"] = (
            "عندها يصبح الطريق أوضح. عندما تكون مستعدًا ابدأ بمهمة واحدة اليوم."
        )
        original = unsafe["sections"][2]["narration"]
        self.assertFalse(apply_safe_short_s3_action_prefix_trim(unsafe))
        self.assertEqual(unsafe["sections"][2]["narration"], original)

    def test_canonical_short_normalizer_is_single_idempotent_owner(self) -> None:
        script = {
            "title": "شورت",
            "sections": [
                {"id": "s1", "narration": "قد يختفي الدافع حين تنتظر الشعور قبل أن تبدأ."},
                {"id": "s2", "narration": "أحيانًا نربط البداية بالشعور المناسب فنؤجل الحركة نفسها."},
                {
                    "id": "s3",
                    "narration": "عندما يبدو الهدف كبيرًا يزيد الاحتكاك. الخطوة الصغيرة أخف على ذهنك وأكثر وضوحًا. الآن اختر مهمة واحدة الآن.",
                },
            ],
        }
        first = normalize_short_script_candidate(script)
        after_first = json.loads(json.dumps(script, ensure_ascii=False))
        second = normalize_short_script_candidate(script)
        self.assertTrue(first["s3_action_prefix_trimmed"])
        self.assertEqual(script, after_first)
        self.assertEqual(
            second,
            {
                "hook_trimmed": False,
                "s3_action_prefix_trimmed": False,
                "s3_trimmed": False,
                "s3_locked_payoff_fallback": False,
            },
        )
        validate_short_script(script)

    def test_pipeline_reapplies_canonical_short_gate_after_text_repair(self) -> None:
        source = inspect.getsource(CleanV2Pipeline.run)
        post_repair = source.split(
            "# A successful bounded repair mutates the script.",
            1,
        )[1].split("identity_runtime =", 1)[0]
        normalize_index = post_repair.index("normalize_short_script_candidate(")
        validate_index = post_repair.index("validate_short_script(script)")
        self.assertLess(normalize_index, validate_index)

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
        self.assertIn("- No channel identity opener, dialogue labels, social CTA", seen["mistral"])
        self.assertIn("preferably 8-16 words", seen["mistral"])
        self.assertIn("split the first sentence on whitespace", seen["mistral"])
        self.assertIn("TARGET 12-16 words and NEVER more than 18", seen["mistral"])
        self.assertIn("if count > 16", seen["mistral"])
        self.assertIn("complete 12-16 word sentence", seen["mistral"])
        self.assertIn("17-18 words as validator headroom only", seen["mistral"])
        self.assertIn("move secondary detail to sentence two", seen["mistral"])
        self.assertIn("MISTRAL_SHORT_S3_COMPLIANCE", seen["mistral"])
        self.assertIn("LOCKED_PLAN.practical_action_ar is host-owned", seen["mistral"])
        self.assertIn("Do NOT write, repeat, paraphrase, or replace it", seen["mistral"])
        self.assertIn("descriptive payoff/explanation sentence", seen["mistral"])
        self.assertIn("ZERO practical-action/imperative markers", seen["mistral"])
        self.assertIn("purely descriptive state/result", seen["mistral"])

    def test_planning_owned_action_replaces_provider_commands_locally(self) -> None:
        brief = _TEMPLATE_FIXTURES["inner_dialogue"]["brief"]
        plan = _plan(_TEMPLATE_FIXTURES["inner_dialogue"]["queries"])
        plan["practical_action_ar"] = "اختر مهمة واحدة واضحة الآن."
        value = {
            "title": "شورت",
            "sections": [
                {"id": "s1", "narration": "قد يختفي الدافع حين تنتظر الشعور قبل أن تبدأ."},
                {"id": "s2", "narration": "أحيانًا نربط البداية بالشعور المناسب فنؤجل الحركة نفسها."},
                {
                    "id": "s3",
                    "narration": (
                        "الخطوة الصغيرة تقلل الاحتكاك وتمنحك نقطة واضحة للعودة. "
                        "اكتب قائمة طويلة الآن. ثم اخرج للمشي."
                    ),
                },
            ],
        }
        accepted = _validate_script_for_brief(value, plan, brief)
        self.assertEqual(
            accepted["sections"][2]["narration"],
            "الخطوة الصغيرة تقلل الاحتكاك وتمنحك نقطة واضحة للعودة. اختر مهمة واحدة واضحة الآن.",
        )
        validate_short_script(accepted)

    def test_run49_planning_action_normalizer_rescues_safe_prefix_and_joined_tail(self) -> None:
        self.assertEqual(
            normalize_short_practical_action("لهذا، اختر مهمة واحدة واضحة الآن."),
            "اختر مهمة واحدة واضحة الآن.",
        )
        self.assertEqual(
            normalize_short_practical_action("اختر مهمة واحدة واضحة ثم راجعها الآن."),
            "اختر مهمة واحدة واضحة.",
        )
        self.assertEqual(
            normalize_short_practical_action("لذلك اختر مهمة واحدة واضحة و اكتبها الآن."),
            "اختر مهمة واحدة واضحة.",
        )

    def test_planning_action_normalizer_does_not_invent_an_unrecognized_action(self) -> None:
        original = "رتب مكتبك الآن."
        self.assertEqual(normalize_short_practical_action(original), original)
        with self.assertRaisesRegex(
            ShortFormatError,
            "short_practical_action_must_begin_with_direct_imperative",
        ):
            validate_short_practical_action(original)

    def test_planning_owned_action_rejects_joined_second_action(self) -> None:
        with self.assertRaisesRegex(
            ShortFormatError,
            "short_practical_action_forbids_joined_second_action",
        ):
            validate_short_practical_action("اختر مهمة واحدة ثم راجعها الآن.")

    def test_run51_planning_action_normalizer_trims_attached_second_imperative(self) -> None:
        original = "حدد خيارًا واحدًا فقط والتزم به لمدة أسبوع."
        self.assertEqual(
            normalize_short_practical_action(original),
            "حدد خيارًا واحدًا فقط.",
        )
        with self.assertRaisesRegex(
            ShortFormatError,
            "short_practical_action_forbids_joined_second_action",
        ):
            validate_short_practical_action(original)

    def test_run51_short_writer_preflights_arabic_surface_grammar(self) -> None:
        context = short_prompt_context(_brief("لماذا تجعلنا كثرة الخيارات أقل حسمًا؟"))
        self.assertIn("demonstrative/noun agreement", context)
        self.assertIn("«مما ...»", context)

    def test_run51_tone_audit_checks_all_sentences_for_grammar_on_first_pass(self) -> None:
        prompt = _scope_clean_v2_tone_prompt(_LEGACY_RELIGIOUS_QUOTE_RULE)
        self.assertIn("SPOKEN ARABIC SURFACE CHECK", prompt)
        self.assertIn("«هذا التوقعات»", prompt)
        self.assertIn("affected section id", prompt)
        self.assertIn("section beginning with «مما ...»", prompt)

    def test_visual_normalizer_makes_expression_query_face_safe(self) -> None:
        plan = _plan(_TEMPLATE_FIXTURES["why_reframe"]["queries"])
        plan["sections"][0]["visual_query_en"] = "frustrated expression writing messy notes at office desk"
        changed = normalize_short_visual_queries(plan)
        self.assertTrue(changed)
        self.assertIn("hands only", plan["sections"][0]["visual_query_en"])
        self.assertNotIn("expression", plan["sections"][0]["visual_query_en"])
        validate_short_visual_queries(plan, _TEMPLATE_FIXTURES["why_reframe"]["brief"])

    def test_run50_consecutive_stationery_is_advisory_not_planning_fatal(self) -> None:
        plan = _plan(_TEMPLATE_FIXTURES["why_reframe"]["queries"])
        plan["sections"][0]["visual_query_en"] = "frustrated worker writing messy notebook"
        plan["sections"][1]["visual_query_en"] = "person pause rewriting notebook plan"
        plan["sections"][1]["visual_query_alt_en"] = "hands writing revised checklist"
        plan["sections"][2]["visual_query_en"] = "focused worker organizing workspace"
        report = validate_short_visual_safety(plan, strict_repetition=True)
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["repetition_advisories"], ["s2"])
        self.assertEqual(report["repetition_owner"], "visual_story_and_visual_qa")

    def test_short_visual_safety_still_fails_closed_on_missing_query(self) -> None:
        plan = _plan(_TEMPLATE_FIXTURES["why_reframe"]["queries"])
        plan["sections"][1]["visual_query_alt_en"] = ""
        with self.assertRaisesRegex(ShortFormatError, "short_visual_safety_missing_query"):
            validate_short_visual_safety(plan, strict_repetition=True)

    def test_mistral_short_safe_s3_normalization_runs_before_provider_validator(self) -> None:
        brief = _TEMPLATE_FIXTURES["inner_dialogue"]["brief"]
        plan = _plan(_TEMPLATE_FIXTURES["inner_dialogue"]["queries"])
        locked_action = plan["practical_action_ar"]
        candidate = {
            "title": "شورت",
            "sections": [
                {"id": "s1", "narration": "قد يختفي الدافع حين تنتظر الشعور قبل أن تبدأ."},
                {"id": "s2", "narration": "أحيانًا نربط البداية بالشعور المناسب فنؤجل الحركة نفسها."},
                {
                    "id": "s3",
                    "s3_payoff": "الخطوة الصغيرة أخف على ذهنك وأكثر وضوحًا.",
                },
            ],
        }
        router = ProviderRouter(
            (
                ProviderAdapter("mistral", lambda _prompt, _tokens: candidate),
            )
        )
        accepted = router.route(
            stage="script",
            prompt=_script_prompt(brief, plan),
            max_tokens=400,
            validator=lambda value: _validate_script_for_brief(value, plan, brief),
        )

        closing = accepted["sections"][2]
        self.assertEqual(
            closing["s3_payoff"],
            "الخطوة الصغيرة أخف على ذهنك وأكثر وضوحًا.",
        )
        self.assertEqual(closing["s3_locked_action"], locked_action)
        self.assertEqual(
            closing["narration"],
            f"{closing['s3_payoff']} {locked_action}",
        )
        validate_short_script(accepted)
        self.assertEqual(
            [(event["provider"], event["result"]) for event in router.events],
            [("mistral", "success")],
        )

    def test_provider_router_rejects_hook_above_rescue_ceiling(self) -> None:
        brief = _TEMPLATE_FIXTURES["inner_dialogue"]["brief"]
        plan = _plan(_TEMPLATE_FIXTURES["inner_dialogue"]["queries"])
        overlong = {
            "title": "شورت",
            "sections": [
                {
                    "id": "s1",
                    "narration": "هذا هوك طويل جدًا لأنه يشرح الفكرة بتفاصيل كثيرة لا نحتاجها الآن ويواصل الكلام حتى يتجاوز الحد الصلب بوضوح من دون حاجة فعلية.",
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
        self.assertEqual(SHORT_HOOK_RESCUE_MAX_WORDS, 20)
        with self.assertRaisesRegex(ShortFormatError, "short_hook_too_long"):
            validate_short_hook_contract(overlong)

    def test_provider_outage_family_accepts_19_word_mistral_hook_as_last_resort(self) -> None:
        brief = _TEMPLATE_FIXTURES["inner_dialogue"]["brief"]
        plan = _plan(_TEMPLATE_FIXTURES["inner_dialogue"]["queries"])
        candidate = {
            "title": "شورت",
            "sections": [
                {
                    "id": "s1",
                    "narration": "قد تفقد الدافع حين تنتظر الشعور المناسب قبل أن تبدأ يومك وتستمر في التأجيل دون فهم ما يمنعك الآن.",
                },
                {
                    "id": "s2",
                    "narration": "أحيانًا يصبح انتظار الشعور المناسب هو ما يؤخر أول حركة بسيطة.",
                },
                {
                    "id": "s3",
                    "narration": "الخطوة الصغيرة أخف على ذهنك وأكثر وضوحًا. اختر مهمة واحدة الآن.",
                },
            ],
        }

        def fail(reason: str, status: int):
            def call(_prompt, _tokens):
                raise ProviderWireFailure(reason, http_status=status)
            return call

        router = ProviderRouter(
            (
                ProviderAdapter("gemini", fail("http_503", 503)),
                ProviderAdapter("groq", fail("http_429", 429)),
                ProviderAdapter("openrouter", lambda _prompt, _tokens: candidate),
                ProviderAdapter("mistral", lambda _prompt, _tokens: candidate),
            )
        )
        router._rate_limited_for_run.add("openrouter")
        with mock.patch.object(providers_module.time, "sleep"):
            accepted = router.route(
                stage="script",
                prompt=_script_prompt(brief, plan),
                max_tokens=400,
                validator=lambda value: _validate_script_for_brief(value, plan, brief),
            )

        report = validate_short_hook_contract(accepted)
        self.assertEqual(report["hook_words"], 19)
        self.assertTrue(report["rescue_headroom_used"])
        self.assertEqual(report["editorial_maximum_words"], 18)
        self.assertEqual(report["rescue_maximum_words"], 20)
        self.assertEqual(
            [(event["provider"], event["result"]) for event in router.events],
            [
                ("gemini", "retrying"),
                ("gemini", "failed"),
                ("groq", "failed"),
                ("openrouter", "unavailable"),
                ("mistral", "success"),
            ],
        )

    def test_short_hook_rescue_headroom_accepts_20_words_without_safe_boundary(self) -> None:
        brief = _TEMPLATE_FIXTURES["inner_dialogue"]["brief"]
        plan = _plan(_TEMPLATE_FIXTURES["inner_dialogue"]["queries"])
        value = {
            "title": "شورت",
            "sections": [
                {
                    "id": "s1",
                    "narration": "قد تفقد الدافع حين تنتظر الشعور المناسب قبل أن تبدأ يومك وتستمر في التأجيل دون فهم ما يمنعك الآن فعلًا.",
                },
                {
                    "id": "s2",
                    "narration": "أحيانًا يصبح انتظار الشعور المناسب هو ما يؤخر أول حركة بسيطة.",
                },
                {
                    "id": "s3",
                    "narration": "الخطوة الصغيرة أخف على ذهنك وأكثر وضوحًا. اختر مهمة واحدة الآن.",
                },
            ],
        }

        accepted = _validate_script_for_brief(value, plan, brief)
        report = validate_short_hook_contract(accepted)
        self.assertEqual(report["hook_words"], 20)
        self.assertTrue(report["rescue_headroom_used"])
        self.assertEqual(report["rescue_maximum_words"], 20)

    def test_small_hook_overrun_trims_only_at_safe_boundary(self) -> None:
        brief = _TEMPLATE_FIXTURES["inner_dialogue"]["brief"]
        plan = _plan(_TEMPLATE_FIXTURES["inner_dialogue"]["queries"])
        value = {
            "title": "شورت",
            "sections": [
                {
                    "id": "s1",
                    "narration": "قد تفقد الدافع حين تنتظر الشعور المناسب قبل أن تبدأ يومك رغم أنك تعرف المطلوب، لكن خطوة صغيرة الآن تكفي فعلًا.",
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
            r"short_hook_too_long words=22 maximum=20",
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
                "tense person pacing around unfinished task under deadline pressure",
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

    def test_short_visual_changes_follow_story_beats_not_duration_thresholds(self) -> None:
        plan = _plan(
            [
                "thoughtful person alone pausing by window",
                "reflective person quietly closing phone",
                "contemplative person calmly taking one step",
            ]
        )
        plan["visual_story"] = {
            "visual_world": "warm neutral natural light no identifiable faces",
            "story_arc": {
                "beginning": "pause",
                "transformation": "choose",
                "arrival": "move",
            },
            "beats": [
                {
                    "id": f"b{index}",
                    "section_id": section_id,
                    "viewer_intent": f"understand beat {index}",
                    "shot_intent": query,
                    "source_preference": "stock_motion",
                }
                for index, (section_id, query) in enumerate(
                    (
                        ("s1", "closed notebook by window"),
                        ("s1", "hand opens notebook"),
                        ("s2", "phone face down on desk"),
                        ("s2", "hand writes one clear task"),
                        ("s3", "shoes at doorway ready to move"),
                        ("s3", "feet walking into warm daylight"),
                    ),
                    1,
                )
            ],
        }
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
        ), mock.patch(
            "clean_v2.media._short_visual_color_compatible",
            return_value=(True, None),
        ):
            clips, rights = source.acquire(
                plan,
                Path(temporary),
                "short",
                9,
                # Deliberately huge values: duration must not add any scene.
                section_estimated_seconds={"s1": 120.0, "s2": 120.0, "s3": 120.0},
            )

        self.assertEqual(len(clips), 6)
        self.assertEqual(len(rights), 6)
        self.assertEqual(
            [row["beat_id"] for row in rights],
            ["b1", "b2", "b3", "b4", "b5", "b6"],
        )
        self.assertEqual(
            [row["section_id"] for row in rights],
            ["s1", "s1", "s2", "s2", "s3", "s3"],
        )
        self.assertLess(SHORT_CUT_DISSOLVE_SECONDS, 0.2)
        trim_source = inspect.getsource(media_module._trim_and_grade_clip)
        self.assertNotIn("_short_motion_filter", trim_source)
        self.assertNotIn("-stream_loop", trim_source)
        self.assertNotIn(
            "_expand_short_visual_sequence",
            inspect.getsource(media_module.render_video),
        )

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
                        "timeline_owner": "measured_gemini38_voice",
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

    def test_ai_still_preference_falls_back_when_free_provider_is_unavailable(self) -> None:
        plan = _plan(
            [
                "thoughtful person alone pausing by window",
                "reflective person quietly closing phone",
                "contemplative person calmly taking one step",
            ]
        )
        plan["visual_story"] = {
            "visual_world": "warm neutral natural light no identifiable faces",
            "story_arc": {
                "beginning": "pause",
                "transformation": "choose",
                "arrival": "move",
            },
            "beats": [
                {
                    "id": f"b{index}",
                    "section_id": f"s{index}",
                    "viewer_intent": f"understand beat {index}",
                    "shot_intent": f"warm practical action {index} no face",
                    "source_preference": "ai_still" if index == 2 else "stock_motion",
                }
                for index in range(1, 4)
            ],
        }
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
                "clean_v2.media._short_visual_color_compatible",
                return_value=(True, None),
            ), mock.patch(
                "clean_v2.media._render_local_short_ai_still",
            ) as render_still, mock.patch(
                "clean_v2.ai_still.generate_cloudflare_ai_still",
                side_effect=ai_still_module.CloudflareAIStillUnavailable(
                    "cloudflare_image_feature_flag_disabled"
                ),
            ):
                clips, rights = source.acquire(
                    plan,
                    output,
                    "short",
                    5,
                    section_estimated_seconds={"s1": 60.0, "s2": 60.0, "s3": 60.0},
                )

        self.assertEqual(len(clips), 3)
        self.assertEqual([row["provider"] for row in rights], ["pexels"] * 3)
        self.assertEqual(rights[1]["source_preference"], "ai_still")
        self.assertEqual(rights[1]["source_actual"], "stock_motion")
        render_still.assert_not_called()

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
    def test_static_cairo_caption_uses_one_arabic_font_and_no_slate(self) -> None:
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
        self.assertEqual(ACCENT_ASS, PRIMARY_ASS)
        self.assertEqual(BODY_FONT, "Cairo")
        self.assertEqual(FOCUS_FONT, BODY_FONT)
        self.assertEqual(FOCUS_FONT_SIZE, BODY_FONT_SIZE)
        self.assertIn("Style: Caption,Cairo", ass)
        self.assertNotIn("Style: Extrusion", ass)
        self.assertNotIn("Style: Shadow", ass)
        self.assertNotIn("Slate", ass)
        self.assertNotIn("Style: Focus", ass)
        self.assertNotIn(r"\kf", ass)
        self.assertIn(PRIMARY_ASS, ass)
        self.assertNotIn(r"\fscx99\fscy99", ass)
        self.assertNotIn("\u202B", ass)
        self.assertEqual(ass.count("Dialogue:"), len(events))
        self.assertNotIn(r"\clip(", ass)
        self.assertNotIn(r"\t(0,", ass)
        self.assertIn(r"\pos(540,1400)", ass)
        self.assertIn(r"\fs", ass)
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
            "identity_events": [
                {"kind": "hook", "start": 0.0, "end": 5.0},
                {"kind": "topic", "start": 5.0, "end": 15.0},
            ],
        }
        events = build_events_from_voice_timeline(script=script, timeline_report=timeline)
        self.assertEqual(len(events), 3)
        self.assertEqual(events[0]["start"], 0.0)
        self.assertEqual(events[-1]["end"], 15.0)
        self.assertEqual(events[0]["role"], "hook")
        self.assertEqual(events[-1]["role"], "payoff")
        self.assertEqual(events[0]["section_id"], "s1")
        self.assertEqual(events[-1]["section_id"], "s3")
        for event in events:
            words = len(str(event["text"]).split())
            self.assertLessEqual(words, CAPTION_MAX_WORDS)
            self.assertGreaterEqual(words, CAPTION_MIN_WORDS)

    def test_local_composition_uses_one_planning_owned_safe_zone_without_provider_calls(self) -> None:
        events = [
            {"start": 0.0, "end": 2.0, "text": "لا تنتظر الدافع", "role": "hook", "section_id": "s1"},
            {"start": 2.0, "end": 4.0, "text": "المشكلة أصغر مما تبدو", "role": "beat", "section_id": "s2"},
            {"start": 4.0, "end": 6.0, "text": "ابدأ بخطوة واحدة", "role": "payoff", "section_id": "s3"},
        ]
        hints = build_composition_hints(events)
        self.assertEqual(COMPOSITION_MODE, "planning_composed_lower_center_safe_v3")
        self.assertEqual({hint["zone"] for hint in hints}, {"lower_center_youtube_safe"})
        self.assertEqual({hint["source"] for hint in hints}, {"planning_composition_contract"})
        self.assertEqual(len({(hint["x"], hint["y"]) for hint in hints}), 1)
        for hint in hints:
            self.assertGreaterEqual(hint["x"], SAFE_X_MIN)
            self.assertLessEqual(hint["x"], SAFE_X_MAX)
            self.assertGreaterEqual(hint["y"], SAFE_Y_MIN)
            self.assertLessEqual(hint["y"], SAFE_Y_MAX)
            self.assertGreaterEqual(hint["font_size"], 150)
            self.assertLessEqual(hint["font_size"], 202)

    def test_hook_type_is_larger_than_dense_beat_and_layout_stays_phrase_stable(self) -> None:
        events = [
            {"start": 0.0, "end": 2.0, "text": "ابدأ الآن", "role": "hook", "section_id": "s1"},
            {"start": 2.0, "end": 4.0, "text": "هذه خمسة كلمات واضحة هنا الآن", "role": "beat", "section_id": "s2"},
            {"start": 4.0, "end": 6.0, "text": "خذ خطوة صغيرة", "role": "payoff", "section_id": "s3"},
        ]
        hints = build_composition_hints(events)
        self.assertGreater(hints[0]["font_size"], hints[1]["font_size"])
        ass = build_rich_ass(events, layout_hints=hints)
        hook_pos = rf"\pos({hints[0]['x']},{hints[0]['y']})"
        self.assertEqual(ass.count(hook_pos), len(events))

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
            self.assertEqual(report["timeline_owner"], "measured_gemini38_voice")
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
            caption_report = dict(report)
            caption_report["identity_events"] = [
                {"kind": "hook", "start": 0.0, "end": 8.0},
                {"kind": "topic", "start": 8.0, "end": 36.0},
            ]
            events = build_events_from_voice_timeline(
                script=script,
                timeline_report=caption_report,
            )
            self.assertEqual(events[0]["start"], 0.0)
            self.assertEqual(events[-1]["end"], report["voice_seconds_measured"])
            self.assertEqual(events[0]["role"], "hook")
            self.assertEqual(events[-1]["role"], "payoff")
            self.assertEqual(len(events), 3)
            for event in events:
                self.assertLessEqual(len(str(event["text"]).split()), CAPTION_MAX_WORDS)


class ShortAudioPolishTests(unittest.TestCase):
    def test_gemini_mastering_is_neutral_loudness_only(self) -> None:
        self.assertEqual(GEMINI_CORRECTIVE_PROFILE, "gemini-3.8-loudness-only-v1")
        self.assertEqual(GEMINI_CORRECTIVE_FILTER, "")

    def test_music_is_minus_25_to_minus_20_db_and_generated_noise_is_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            narration = root / "narration-mastered.wav"
            raw_music = root / "music-source.wav"
            music = root / "music.wav"
            for path, frequency, volume in (
                (narration, 220, -10),
                (raw_music, 330, -12),
            ):
                subprocess.run(
                    [
                        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                        "-f", "lavfi", "-i",
                        f"sine=frequency={frequency}:sample_rate=48000:duration=2.2",
                        "-af", f"volume={volume}dB",
                        "-c:a", "pcm_s16le", str(path),
                    ],
                    check=True,
                )
            narration_mean = _measure_mean_db(narration)
            music_report = _normalize_relative(
                src=raw_music,
                dest=music,
                narration_mean_db=narration_mean,
                target_relative_db=MUSIC_TARGET_REL_DB,
                minimum_relative_db=MUSIC_MIN_REL_DB,
                maximum_relative_db=MUSIC_MAX_REL_DB,
            )
            self.assertEqual((MUSIC_MIN_REL_DB, MUSIC_TARGET_REL_DB, MUSIC_MAX_REL_DB), (-21.0, -19.0, -17.0))
            self.assertGreaterEqual(music_report["relative_to_narration_db"], MUSIC_MIN_REL_DB)
            self.assertLessEqual(music_report["relative_to_narration_db"], MUSIC_MAX_REL_DB)
            with self.assertRaisesRegex(RuntimeError, "procedural_noise_music_disabled"):
                _generate_raw_music(root / "forbidden.wav", 2.0)
            with self.assertRaisesRegex(RuntimeError, "generated_sfx_disabled"):
                _generate_raw_sfx(root / "forbidden-sfx.wav", frequency=523.25)
            self.assertNotIn("anoisesrc", inspect.getsource(_generate_raw_music))
            self.assertNotIn("anoisesrc", inspect.getsource(_generate_raw_sfx))

    def test_generation_failure_is_fail_safe_not_production_block(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            final_path = root / "final.mp4"
            narration = root / "narration-mastered.wav"
            final_path.write_bytes(b"fixture")
            narration.write_bytes(b"fixture")

            with (
                mock.patch(
                    "clean_v2.short_audio_polish._measure_mean_db",
                    return_value=-18.0,
                ),
                mock.patch(
                    "clean_v2.short_audio_polish._topic_window",
                    return_value=(5.0, 10.0),
                ),
                mock.patch(
                    "clean_v2.short_audio_polish.select_music_track",
                    return_value=(None, {"allow_download": False}),
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
        self.assertIn("GEMINI 3.8 SPOKEN ARABIC WRITING CONTRACT", prompt)
        self.assertIn("ONLY the minimum Arabic diacritic marks", prompt)
        self.assertIn("punctuation as performance notation", prompt)
        self.assertIn("Gemini 3.8 reads transcript text verbatim", prompt)
        self.assertIn("Performance direction belongs to structured", prompt)
        self.assertIn("For inner_dialogue, keep one Charon voice", prompt)
        self.assertIn("never more than one such event in a short section", prompt)
        self.assertIn("Do not write toward a target duration", prompt)
        self.assertIn("measured mastered voice owns the final runtime", prompt)
        self.assertNotIn("30-45 seconds", prompt)
        self.assertNotIn("34-38 second", prompt)
        self.assertIn("selected_template=inner_dialogue", prompt)
        self.assertIn("social CTA remains visual-only", prompt)
        self.assertIn("IDENTITY_SEQUENCE is also HOST-MANAGED", prompt)
        self.assertIn(SHORT_CHANNEL_DEFINITION, prompt)
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
                self.last_provider = "gemini-3.8:Charon"
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

        self.assertEqual(report["voice_provider"], "gemini-3.8:Charon")
        self.assertFalse(report["voice_fallback_used"])
        self.assertTrue(voice.primary_only_flags)
        self.assertTrue(all(voice.primary_only_flags))

    def test_mid_run_gemini_failure_fails_closed_without_lite_restart(self) -> None:
        calls = {"count": 0}

        def fake_gemini38(api_key, transcript, output_path, **_kwargs):
            del api_key, transcript
            calls["count"] += 1
            if calls["count"] == 1:
                Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                Path(output_path).write_bytes(b"C" * 2048)
                return Path(output_path)
            raise RuntimeError("http 429")

        sections = [
            {"id": "s1", "narration": "هذه جملة أولى واضحة للاختبار."},
            {"id": "s2", "narration": "هذه جملة ثانية واضحة للاختبار."},
        ]

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            narration = root / "narration.wav"
            synth = GeminiOnlyVoiceSynthesizer(
                "gemini-key",
                tts_model="gemini-3.8-flash-tts",
            )
            with (
                mock.patch.object(
                    media_module,
                    "_gemini38_synthesize",
                    side_effect=fake_gemini38,
                ),
                mock.patch.object(
                    media_module,
                    "_charon_retry_delay",
                    return_value=None,
                ),
            ):
                with self.assertRaises(VoiceInfrastructureError):
                    _synthesize_sectioned_voice(
                        synth,
                        sections,
                        narration,
                        require_charon_only=True,
                    )

            persisted = json.loads(
                (root / "voice-sections.json").read_text(encoding="utf-8")
            )
            first_section_preserved = (root / "audio" / "01.wav").is_file()

        # s1 succeeds once; s2 fails once and stops. The successful primary
        # chunk is not discarded and there is no full lite-model replay.
        self.assertEqual(calls["count"], 2)
        self.assertTrue(first_section_preserved)
        self.assertEqual(persisted["status"], "failed")
        self.assertEqual(persisted["reason"], "gemini_3_8_voice_failed_closed")
        self.assertFalse(narration.exists())

    def test_short_gemini38_passes_viewer_facing_performance_direction_without_rewriting(self) -> None:
        captured: dict[str, object] = {}

        def fake_synthesize(api_key, transcript, output_path, **kwargs):
            captured.update(
                {
                    "api_key": api_key,
                    "transcript": transcript,
                    **kwargs,
                }
            )
            Path(output_path).write_bytes(b"W" * 2048)
            return Path(output_path)

        with tempfile.TemporaryDirectory() as temporary:
            synth = GeminiOnlyVoiceSynthesizer(
                "gemini-key",
                tts_model="gemini-3.8-flash-tts",
            )
            transcript = "ابدأ بخطوة واحدة واضحة الآن."
            with (
                mock.patch.object(media_module, "_gemini38_synthesize", side_effect=fake_synthesize),
            ):
                result = synth.synthesize(
                    transcript,
                    Path(temporary) / "out.wav",
                    primary_only=True,
                )

        self.assertEqual(result.name, "out.wav")
        self.assertEqual(captured["transcript"], transcript)
        self.assertEqual(captured["model"], "gemini-3.8-flash-tts")
        self.assertEqual(captured["primary_voice"], "Charon")
        self.assertEqual(captured["questioner_voice"], "Orus")

    def test_gemini38_failure_never_substitutes_another_voice(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            synth = GeminiOnlyVoiceSynthesizer(
                "gemini-key",
                tts_model="gemini-3.8-flash-tts",
            )
            with (
                mock.patch.object(
                    media_module,
                    "_gemini38_synthesize",
                    side_effect=RuntimeError("http_503"),
                ),
                mock.patch.object(media_module, "_charon_retry_delay", return_value=None),
            ):
                with self.assertRaises(VoiceInfrastructureError) as raised:
                    synth.synthesize(
                        "نص قصير",
                        Path(temporary) / "out.wav",
                        primary_only=True,
                    )
            self.assertEqual(
                raised.exception.secondary_reason,
                "gemini_3_8_only_fail_closed_no_fallback",
            )
            self.assertIsNone(synth.last_provider)
            self.assertFalse(synth.fallback_used)

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



class SharedColorIdentityRegressionTests(unittest.TestCase):
    def test_master_look_uses_navy_shadows_and_warm_highlights(self) -> None:
        shadow = media_module._master_look_value(0.18, 0.18, 0.18)
        highlight = media_module._master_look_value(0.88, 0.88, 0.88)
        self.assertGreater(shadow[2], shadow[0])
        self.assertGreater(highlight[0], highlight[2])
        self.assertEqual(media_module.COLOR_MATCH_STRENGTH, 0.70)
        self.assertLess(media_module.MASTER_LOOK_SATURATION, 0.90)

    def test_single_clip_uses_fixed_channel_target_not_episode_stock_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            clip = root / "clip.mp4"
            clip.write_bytes(b"fixture")
            measured = media_module._RgbStats(
                mean_r=170.0,
                mean_g=150.0,
                mean_b=145.0,
                std_r=35.0,
                std_g=36.0,
                std_b=34.0,
            )
            with mock.patch(
                "clean_v2.media._sample_rgb_stats",
                return_value=measured,
            ):
                filters = media_module._build_reference_color_plan([clip], root)
            self.assertTrue(filters[str(clip)])
            report = json.loads((root / "color-match.json").read_text(encoding="utf-8"))
            self.assertEqual(report["source"], "clean-v2-fixed-channel-palette-match")
            self.assertIsNone(report["reference_file"])
            self.assertEqual(report["clips"][0]["mode"], "fixed_channel_target")
            self.assertEqual(report["target_stats"]["mean_b"], 118.0)

    def test_shared_finish_is_always_applied_after_master_lut(self) -> None:
        source = inspect.getsource(media_module.render_video)
        self.assertIn("CINEMATIC_FINISH_FILTER", source)
        self.assertNotIn(
            'if any(str(value or "").strip() for value in grade_filters.values())',
            source,
        )
        self.assertIn("navy-gold-master-v5.cube", source)
        self.assertEqual(
            media_module.CINEMATIC_FINISH_VERSION,
            "clean-v2-navy-gold-depth-finish-v5",
        )


class Run58ShortRegressionTests(unittest.TestCase):
    def _base_script(self) -> dict:
        return {
            "title": "لماذا تشلّنا كثرة الخيارات؟",
            "sections": [
                {
                    "id": "s1",
                    "narration": "لماذا يزداد ترددنا في اتخاذ القرار كلما اتسعت أمامنا قائمة البدائل؟",
                },
                {
                    "id": "s2",
                    "narration": "كل بديل إضافي يفرض مقارنة جديدة حتى تنفد طاقتنا قبل الوصول إلى نتيجة ملموسة.",
                },
                {
                    "id": "s3",
                    "narration": "الحسم يصبح أسهل حين تضيق مساحة المقارنة. حدد معيارًا واحدًا فقط لاختيارك القادم.",
                },
            ],
        }

    def test_run58_rejects_attached_second_imperative_inside_s3_action_sentence(self) -> None:
        script = self._base_script()
        script["sections"][2]["narration"] = (
            "توقف عن البحث المستمر عن الكمال المفقود، "
            "وحدد معيارًا واحدًا فقط لاختيارك القادم."
        )
        with self.assertRaisesRegex(
            ShortFormatError,
            "short_s3_forbids_joined_second_action",
        ):
            validate_short_script(script)

    def test_run58_locked_payoff_never_salvages_dependent_bel_fragment(self) -> None:
        script = self._base_script()
        script["sections"][2]["narration"] = (
            "البداية الصغيرة تكسر الجمود. "
            "حدد معيارًا واحدًا فقط لاختيارك القادم."
        )
        original = script["sections"][2]["narration"]
        self.assertFalse(
            apply_safe_short_s3_locked_payoff_fallback(
                script,
                "الحسم لا يأتي من اختيار الأفضل، بل من وضع معايير كافية والالتزام بها.",
            )
        )
        self.assertEqual(script["sections"][2]["narration"], original)
        self.assertNotIn("بل من وضع معايير كافية", script["sections"][2]["narration"])


if __name__ == "__main__":
    unittest.main()

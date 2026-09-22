from __future__ import annotations

import inspect
import json
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
    _planning_prompt,
    _script_prompt,
    _short_identity_not_applicable,
    _synthesize_sectioned_voice,
    _validate_script_for_brief,
)
from clean_v2 import media as media_module
from clean_v2.media import (
    GeminiPrimaryPiperFallbackSynthesizer,
    SHORT_CHARON_STYLE,
    VoiceInfrastructureError,
)
from clean_v2.providers import ProviderAdapter, ProviderRouter
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
    SHORT_MAX_SECONDS,
    SHORT_MIN_SECONDS,
    SHORT_SECTION_COUNT,
    SHORT_TARGET_SECONDS,
    SHORT_WIDTH,
    ShortFormatError,
    TEMPLATE_VISUAL_QUERY_DIRECTIVES,
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
            "inner_dialogue_not_reflective",
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
                self.assertIn("visibly different dominant actions or states", prompt)
                self.assertIn("active friction/decision", prompt)
                self.assertIn(f"selected_template={expected}", prompt)
                self.assertIn("return an empty CTA string", prompt)
                self.assertEqual(selection["extra_ai_calls"], 0)


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
        self.assertLessEqual(report["hook_words"], 12)

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
        for example in ("ابدأ بـ...", "جرّب أن...", "افعل...", "اختر...", "اكتب..."):
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
        direct_action["sections"][2]["narration"] = "ابدأ بخطوة صغيرة تستطيع تنفيذها الآن."
        report = validate_short_script(direct_action)
        self.assertEqual(report["practical_action_sentences"], 1)
        self.assertEqual(report["practical_action_markers"], 1)

        double_action = json.loads(json.dumps(no_action, ensure_ascii=False))
        double_action["sections"][2]["narration"] = "اكتب كلمة واحدة على ورقة ثم اخرج للمشي."
        with self.assertRaisesRegex(
            ShortFormatError,
            r"short_s3_requires_one_action_only imperative_markers=2",
        ):
            validate_short_script(double_action)

    def test_provider_router_rejects_technically_successful_hook_over_12_words(self) -> None:
        brief = _TEMPLATE_FIXTURES["inner_dialogue"]["brief"]
        plan = _plan(_TEMPLATE_FIXTURES["inner_dialogue"]["queries"])
        overlong = {
            "title": "شورت",
            "sections": [
                {
                    "id": "s1",
                    "narration": "هذا هوك طويل جدًا لأنه يحتوي كلمات كثيرة أكثر من الحد المسموح للشورت الآن.",
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
        self.assertEqual(SHORT_HOOK_MAX_WORDS, 12)
        with self.assertRaisesRegex(ShortFormatError, "short_hook_too_long"):
            validate_short_hook_contract(overlong)

    def test_duration_and_frame_contract_are_hard_bounds(self) -> None:
        self.assertEqual(SHORT_MIN_SECONDS, 7.0)
        self.assertEqual(SHORT_TARGET_SECONDS, 15.0)
        self.assertEqual(SHORT_MAX_SECONDS, 30.0)
        for seconds in (SHORT_MIN_SECONDS, SHORT_TARGET_SECONDS, SHORT_MAX_SECONDS):
            self.assertEqual(validate_short_duration(seconds, phase="test"), seconds)
        for seconds in (SHORT_MIN_SECONDS - 0.001, SHORT_MAX_SECONDS + 0.001):
            with self.assertRaisesRegex(ShortFormatError, "short_duration_out_of_range"):
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
    def test_transition_word_creates_exactly_one_non_hook_dark_slate(self) -> None:
        events = [
            {
                "start": 0.0,
                "end": 3.8,
                "text": "قد يختفي الدافع حين تنتظر الشعور قبل أن تبدأ.",
                "role": "hook",
            },
            {
                "start": 3.8,
                "end": 8.4,
                "text": "لكن الحقيقة أن البداية الصغيرة تغيّر اتجاه اللحظة.",
                "role": "beat",
            },
            {
                "start": 8.4,
                "end": 13.5,
                "text": "ابدأ بخطوة واحدة تستطيع تنفيذها الآن.",
                "role": "payoff",
            },
        ]

        validated = validate_progressive_text(events)
        slate_index = choose_dark_slate_index(events, validated)
        ass = build_rich_ass(events, slate_index=slate_index)

        self.assertEqual(slate_index, 1)
        self.assertNotEqual(slate_index, 0)
        self.assertEqual(MAX_DARK_SLATES, 1)
        self.assertIn("Style: SlateFocus", ass)
        self.assertEqual(ass.count("SlateFocus,,0,0,0"), 1)
        self.assertIn(r"\fscx103\fscy103", ass)
        self.assertIn(r"{\an5\pos(", ass)
        self.assertNotIn(r"{{\an5\pos(", ass)
        self.assertEqual(ACCENT_ASS, "&H005BA8D7")
        self.assertEqual(BODY_FONT, "Noto Sans Arabic")
        self.assertEqual(FOCUS_FONT, "Noto Kufi Arabic")
        self.assertGreater(FOCUS_FONT_SIZE, BODY_FONT_SIZE)
        self.assertGreaterEqual(BODY_FONT_SIZE, 70)
        self.assertIn(r"\N", ass)

    def test_body_focus_split_preserves_authored_words(self) -> None:
        text = "لكن الحقيقة أن البداية الصغيرة تغيّر اتجاه اللحظة"
        body, focus = split_focus_phrase(text, "beat")
        self.assertEqual(" ".join((body + " " + focus).split()), text)


class ShortVoiceOwnedTimelineTests(unittest.TestCase):
    def test_cohort_attempt_3_31_03_fails_closed_without_regeneration_or_extension(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            narration = root / "narration-mastered.wav"
            narration.write_bytes(b"fixture")

            with mock.patch(
                "clean_v2.short_voice_owned_timeline.probe_duration",
                return_value=31.03,
            ), mock.patch(
                "clean_v2.short_voice_owned_timeline.retime_events",
                wraps=retime_events,
            ) as retime_mock:
                with self.assertRaisesRegex(
                    ShortVoiceTimelineError,
                    r"VOICE_EXCEEDS_SHORT_MAX voice=31\.030s max=30\.000s planning_repair_required=true",
                ) as raised:
                    build_short_voice_owned_timeline(
                        output_dir=root,
                        narration_path=narration,
                    )

            self.assertEqual(raised.exception.report["status"], "block")
            self.assertEqual(
                raised.exception.report["reason"],
                "VOICE_EXCEEDS_SHORT_MAX",
            )
            self.assertTrue(raised.exception.report["planning_repair_required"])
            self.assertFalse(
                raised.exception.report["tts_regeneration_for_duration"]
            )
            self.assertEqual(raised.exception.report["duration_repair_attempts"], 0)
            retime_mock.assert_not_called()

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
                "narration-mastered.wav": 24.0,
                "01.wav": 5.0,
                "02.wav": 7.0,
                "03.wav": 12.0,
            }

            with mock.patch(
                "clean_v2.short_voice_owned_timeline.probe_duration",
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
                {"s1": 5.0, "s2": 7.0, "s3": 12.0},
            )
            self.assertEqual(
                [(item["start"], item["end"]) for item in report["section_events"]],
                [(0.0, 5.0), (5.0, 12.0), (12.0, 24.0)],
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
            self.assertEqual(
                [(item["start"], item["end"]) for item in events],
                [(0.0, 5.0), (5.0, 12.0), (12.0, 24.0)],
            )
            self.assertEqual(events[-1]["end"], report["voice_seconds_measured"])


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
    def test_short_script_prompt_keeps_compact_30s_ceiling_and_reuses_template_context(self) -> None:
        fixture = _TEMPLATE_FIXTURES["inner_dialogue"]
        prompt = _script_prompt(fixture["brief"], _plan(fixture["queries"]))
        self.assertIn("22-40 spoken Arabic words", prompt)
        self.assertIn("legacy 15-second target", prompt)
        self.assertIn("must never exceed 30 seconds", prompt)
        self.assertIn("selected_template=inner_dialogue", prompt)
        self.assertIn("CTA is", prompt)
        self.assertIn("fully disabled", prompt)
        self.assertIn("concrete felt friction", prompt)
        self.assertIn("resolve the SAME tension/question", prompt)
        self.assertIn("must not append a second action", prompt)

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

        self.assertTrue(result.is_file())
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

    def test_short_identity_is_local_not_applicable_with_zero_provider_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            report = _short_identity_not_applicable(output)
            self.assertEqual(report["status"], "not_applicable")
            self.assertEqual(report["provider_calls_added"], 0)
            persisted = json.loads(
                (output / "narrative-identity.json").read_text(encoding="utf-8")
            )
            self.assertEqual(persisted["transitions"], [])
            self.assertEqual(persisted["opener"], "")
            self.assertEqual(persisted["closer"], "")

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


if __name__ == "__main__":
    unittest.main()

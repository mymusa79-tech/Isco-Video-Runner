from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clean_v2.contracts import ContractError, SUPPORTED_FORMATS, validate_plan
from clean_v2.identity_sequence import (
    PODCAST_CHANNEL_DEFINITION,
    PRAYER_SENTENCE,
    assert_spoken_identity,
    inject_spoken_identity,
)
from clean_v2.media import GeminiOnlyVoiceSynthesizer, VoiceInfrastructureError, _gemini38_dialogue_turns
from clean_v2.pipeline import (
    CleanV2Pipeline,
    PODCAST_LISTENER_PROXY_QUESTION_MAX_WORDS,
    PODCAST_LISTENER_PROXY_QUESTION_RESCUE_MAX_WORDS,
    _factuality_repair_prompt,
    _isolate_podcast_promo_unit,
    _planning_prompt,
    _run_podcast_derived_short_lite,
    _script_prompt,
    _select_podcast_promo_excerpt,
    _tone_repair_prompt,
    _validate_podcast_listener_proxy_script,
    normalize_podcast_listener_proxy_script,
)
from clean_v2.podcast_key_text import PodcastKeyTextError, apply_podcast_key_text
from clean_v2.podcast_key_text import build_ass as build_podcast_key_text_ass
from clean_v2.podcast_key_text import build_events as build_podcast_key_text_events
from clean_v2.visual_qa import _apply_cultural_islamic_policy
from scripts import clean_v2_release_delivery as delivery
from scripts.telegram_clean_v2_control import (
    _request_hash,
    _scope_research_instruction,
    materialize_brief,
    scope_keyboard,
)
from scripts.telegram_clean_v2_notify import milestone_messages, started_text


class PodcastFormatTests(unittest.TestCase):
    def _plan(self, count: int) -> dict:
        return {
            "title": "عنوان",
            "promise": "وعد واضح",
            "cta": "شاركها مع من يحتاجها.",
            "sections": [
                {
                    "id": f"s{index}",
                    "heading": f"قسم {index}",
                    "purpose": f"تقدم الفكرة في القسم {index}",
                    "visual_query_en": f"quiet lived in room detail {index}",
                }
                for index in range(1, count + 1)
            ],
        }

    def test_podcast_is_a_first_class_clean_v2_format_with_flexible_sections(self) -> None:
        self.assertIn("podcast", SUPPORTED_FORMATS)
        self.assertEqual(len(validate_plan(self._plan(2), {"format": "podcast"})["sections"]), 2)
        self.assertEqual(len(validate_plan(self._plan(5), {"format": "podcast"})["sections"]), 5)
        with self.assertRaisesRegex(ContractError, "between 2 and 5 for podcast"):
            validate_plan(self._plan(1), {"format": "podcast"})
        with self.assertRaisesRegex(ContractError, "between 2 and 5 for podcast"):
            validate_plan(self._plan(6), {"format": "podcast"})

    def test_podcast_prompt_prioritizes_depth_audio_only_and_sparse_visuals(self) -> None:
        brief = {
            "approved_by_user": True,
            "approved_topic": "لماذا نعود إلى عادة نعرف أنها تؤذينا؟",
            "format": "podcast",
            "language": "ar",
            "research_pack": [],
        }
        planning = _planning_prompt(brief)
        script = _script_prompt(brief, self._plan(3))
        planning_compact = " ".join(planning.split())
        self.assertIn("genuinely worthwhile central question", planning_compact)
        self.assertIn("generic self-help", planning)
        self.assertIn("ONE visual beat per section", planning)
        self.assertIn("audio must", planning)
        self.assertIn("خارج النص", planning)
        self.assertIn("listener's understanding must", planning)
        self.assertIn("genuine central question or contradiction", planning)
        self.assertIn("visual motif remains supportive and non-essential", planning)
        self.assertIn("without erasing their separate pacing and audio rules", planning)
        self.assertIn("dialogue_qa", planning)
        self.assertIn("narrative_format=dialogue_qa", planning)
        self.assertIn("podcast_listener_proxy_qa", planning)
        self.assertIn("A listener-proxy turn does NOT force a scene cut", planning)
        self.assertNotIn("narrative_format=question_answer", planning)
        self.assertNotIn("payoff_answer must be a descriptive resolution", planning)
        self.assertIn("fixed Gemini 3.8", script)
        self.assertIn("fixed Gemini 3.8", script)
        self.assertIn("Never invent first-person", script)
        self.assertIn("10-30 minutes is a normal editorial range, never an acceptance gate", script)
        self.assertIn("actual synthesized voice owns the final duration completely", script)
        self.assertIn("audio alone", script)
        self.assertIn("article, lecture, news script", script)
        self.assertIn("speaking simply to one listener", script)
        self.assertIn("numbered-list", script)
        self.assertIn("s2 must add a mechanism, cause, or distinction", script)
        self.assertIn("PODCAST HOOK QUALITY", script)
        self.assertIn("LONGFORM RETENTION PREFLIGHT", script)
        self.assertIn("hook_genericness=false", script)
        self.assertIn("hook_body_continuity=true", script)
        self.assertIn("payoff_resolves_hook=true", script)
        self.assertIn("BAD progression", script)
        self.assertIn("GOOD progression", script)
        self.assertIn("hook_genericness", script)
        self.assertIn("hook_honesty", script)
        self.assertIn("hook_specificity", script)
        self.assertIn("specific to THIS approved episode", script)
        self.assertIn("s3, when present, must derive a new implication or resolution from s2", script)
        self.assertIn("generic advice and synonymous restatement are not progression", script)
        self.assertIn("GEMINI 3.8 SPOKEN ARABIC WRITING CONTRACT", script)
        self.assertIn("not fully vocalized textbook Arabic", script)
        self.assertIn("ONLY the minimum Arabic diacritic marks", script)
        self.assertIn("Preserve meaningful diacritics", script)
        self.assertIn("punctuation as performance notation", script)
        self.assertIn("spoken comfortably in one breath", script)
        self.assertNotIn("HARD maximum of 18 Arabic words", script)

        film_script = _script_prompt(
            {**brief, "format": "film"},
            self._plan(5),
        )
        self.assertIn("GEMINI 3.8 SPOKEN ARABIC WRITING CONTRACT", film_script)
        self.assertIn("ONLY the minimum Arabic diacritic marks", film_script)
        self.assertIn("punctuation as performance notation", film_script)
        self.assertIn("all spoken formats", film_script)
        self.assertIn("LONGFORM RETENTION PREFLIGHT", film_script)
        self.assertIn("hook_body_continuity=true", film_script)
        self.assertIn("payoff_resolves_hook=true", film_script)

    def test_narrative_format_fidelity_selfcheck_appears_for_film_and_podcast_only(self) -> None:
        # Run #47 (Telegram film) was blocked post-repair on "editorial_promise_continuity:
        # narrative format mismatch": the locked plan declared narrative_format=question_answer
        # but the delivered narration read as a flat sequence of instructional steps, never
        # posing a new question after the hook. Neither the script prompt nor the repair prompt
        # ever told the Writer to check the ACTUAL narration against writing_shape's described
        # behavior, only to keep the label - so this closes that gap explicitly.
        brief = {
            "approved_by_user": True,
            "approved_topic": "لماذا نعود إلى عادة نعرف أنها تؤذينا؟",
            "format": "podcast",
            "language": "ar",
            "research_pack": [],
        }
        podcast_script = _script_prompt(brief, self._plan(3))
        self.assertIn("NARRATIVE FORMAT FIDELITY", podcast_script)
        self.assertIn("do not merely keep the narrative_format LABEL", podcast_script)
        self.assertIn(
            "must still pose one new sincere question that sharpens or deepens the SAME inquiry",
            podcast_script,
        )

        film_script = _script_prompt({**brief, "format": "film"}, self._plan(5))
        self.assertIn("NARRATIVE FORMAT FIDELITY", film_script)
        self.assertIn(
            "must still pose one new sincere question that sharpens or deepens the SAME inquiry",
            film_script,
        )

        short_brief = {**brief, "format": "short"}
        short_plan = {
            "title": "عنوان",
            "sections": [
                {"id": "s1", "heading": "1", "purpose": "غرض", "visual_query_en": "shot"},
                {"id": "s2", "heading": "2", "purpose": "غرض", "visual_query_en": "shot"},
                {"id": "s3", "heading": "3", "purpose": "غرض", "visual_query_en": "shot"},
            ],
        }
        short_script = _script_prompt(short_brief, short_plan)
        self.assertNotIn("NARRATIVE FORMAT FIDELITY", short_script)

    def test_planning_prompt_shapes_section_headings_to_the_locked_narrative_format(self) -> None:
        film_brief = {
            "approved_by_user": True,
            "approved_topic": "كيف تستعيد تركيزك بعد أيام من التشتت؟",
            "format": "film",
            "language": "ar",
            "research_pack": [],
        }
        planning = _planning_prompt(film_brief)
        self.assertIn("narrative_format=question_answer", planning)
        self.assertIn("the locked narrative_format above is question_answer", planning)
        self.assertIn(
            "phrase each section after the first as the new sincere question", planning
        )
        self.assertIn("الخطوة الأولى", planning)

        podcast_brief = {
            "approved_by_user": True,
            "approved_topic": "لماذا نعود إلى عادة نعرف أنها تؤذينا؟",
            "format": "podcast",
            "language": "ar",
            "research_pack": [],
        }
        podcast_planning = _planning_prompt(podcast_brief)
        self.assertIn("narrative_format=dialogue_qa", podcast_planning)
        self.assertNotIn("narrative_format=question_answer", podcast_planning)
        self.assertIn(
            "phrase headings/purposes so they already perform writing_shape's described behavior above",
            podcast_planning,
        )
        self.assertNotIn(
            "phrase each section after the first as the new sincere question",
            podcast_planning,
        )

    def test_tone_repair_prompt_restores_locked_narrative_format_writing_shape_on_mismatch(
        self,
    ) -> None:
        film_brief = {
            "approved_by_user": True,
            "approved_topic": "كيف تستعيد تركيزك بعد أيام من التشتت؟",
            "format": "film",
            "language": "ar",
            "research_pack": [],
        }
        plan = {**self._plan(3), "narrative_format": "question_answer"}
        script = {
            "title": "عنوان",
            "sections": [
                {"id": "s1", "narration": "هل تشتت انتباهك مؤخرًا؟"},
                {"id": "s2", "narration": "الخطوة الأولى: اختر مهمة واحدة."},
                {"id": "s3", "narration": "الخطوة الثانية: التزم بها."},
            ],
        }
        prompt = _tone_repair_prompt(
            brief=film_brief,
            plan=plan,
            script=script,
            identity={"opener": "", "closer": ""},
            cta_plan={"spoken_text": "", "anchor_section_id": ""},
            revision_note=(
                "editorial_promise_continuity: narrative format mismatch: question_answer plan "
                "but s2/s3 read as flat instructional steps with no question"
            ),
        )
        self.assertIn("narrative_format=question_answer", prompt)
        self.assertIn(
            "asks sincere progressively deeper questions and answers them; never a flat FAQ",
            prompt,
        )
        self.assertIn("Rewrite any section that reads as a flat instructional step", prompt)

        # A plan with no narrative_format at all (e.g. short) must not gain this guidance.
        no_format_prompt = _tone_repair_prompt(
            brief=film_brief,
            plan=self._plan(3),
            script=script,
            identity={"opener": "", "closer": ""},
            cta_plan={"spoken_text": "", "anchor_section_id": ""},
            revision_note="s2 and s3 repeat the same idea instead of advancing the central question",
        )
        self.assertNotIn("Rewrite any section that reads as a flat instructional step", no_format_prompt)

    def test_podcast_prompt_states_the_exact_18_word_a_turn_cap(self) -> None:
        # Run #26 failed with "podcast_listener_proxy_question_too_long words=20
        # maximum=18" - the validator's hard cap was never actually stated as a
        # number anywhere in the prompt ("keep A concise", "12-24 Arabic words"
        # for the hook, "short" for every other turn), so a provider had no way
        # to know the real limit it would be judged against.
        brief = {
            "approved_by_user": True,
            "approved_topic": "لماذا نعود إلى عادة نعرف أنها تؤذينا؟",
            "format": "podcast",
            "language": "ar",
            "research_pack": [],
        }
        script = _script_prompt(brief, self._plan(3))
        self.assertIn("18 Arabic words", script)
        self.assertIn("Every single A turn", script)

        film_script = _script_prompt({**brief, "format": "film"}, self._plan(5))
        self.assertNotIn("18 Arabic words", film_script)

    def test_podcast_tone_repair_prompt_requires_forward_reasoning_without_broadening_other_formats(self) -> None:
        podcast_brief = {
            "approved_by_user": True,
            "approved_topic": "لماذا نعود إلى عادة نعرف أنها تؤذينا؟",
            "format": "podcast",
            "language": "ar",
            "research_pack": [],
        }
        plan = self._plan(3)
        script = {
            "title": "عنوان",
            "sections": [
                {"id": "s1", "narration": "لماذا نعود إلى ما نعرف أنه يضرنا؟"},
                {"id": "s2", "narration": "نعود لأن الفكرة ما زالت كما هي."},
                {"id": "s3", "narration": "وهذا يعيدنا إلى الفكرة نفسها."},
            ],
        }
        kwargs = {
            "plan": plan,
            "script": script,
            "identity": {"opener": "", "closer": ""},
            "cta_plan": {"spoken_text": "", "anchor_section_id": ""},
            "revision_note": "s2 and s3 repeat the same idea instead of advancing the central question",
        }
        podcast_prompt = _tone_repair_prompt(brief=podcast_brief, **kwargs)
        self.assertIn("fix progression semantically, not cosmetically", podcast_prompt)
        self.assertIn("s2 must add a mechanism, cause, or distinction", podcast_prompt)
        self.assertIn("s3, when present, must derive a new implication or resolution from s2", podcast_prompt)
        self.assertIn("generic advice or paraphrase is not a payoff", podcast_prompt)
        self.assertIn("GEMINI 3.8 SPOKEN ARABIC WRITING CONTRACT", podcast_prompt)
        self.assertIn("keep intentional minimal", podcast_prompt)

        film_repair_prompt = _tone_repair_prompt(
            brief={**podcast_brief, "format": "film"},
            **kwargs,
        )
        self.assertIn("GEMINI 3.8 SPOKEN ARABIC WRITING CONTRACT", film_repair_prompt)
        self.assertIn("keep intentional minimal", film_repair_prompt)

        film_prompt = _tone_repair_prompt(
            brief={**podcast_brief, "format": "film"},
            **kwargs,
        )
        self.assertIn("fix progression semantically, not cosmetically", film_prompt)
        self.assertIn("For film and podcast", film_prompt)

        podcast_factuality = _factuality_repair_prompt(
            brief=podcast_brief,
            **kwargs,
        )
        film_factuality = _factuality_repair_prompt(
            brief={**podcast_brief, "format": "film"},
            **kwargs,
        )
        for repair_prompt in (podcast_factuality, film_factuality):
            self.assertIn("GEMINI 3.8 SPOKEN ARABIC WRITING CONTRACT", repair_prompt)
            self.assertIn("minimal diacritics", repair_prompt)
            self.assertIn("comfortable to say in one breath", repair_prompt)

    def test_podcast_uses_v8_visual_branding_without_repeating_brand_in_speech(self) -> None:
        sections = [
            {"id": "s1", "narration": "لماذا نعود إلى ما قررنا تركه؟ نبدأ من وظيفة السلوك نفسه."},
            {"id": "s2", "narration": "عندما نفهم الوظيفة يصبح التغيير أوضح."},
        ]
        closer = "وهنا تنتهي الفكرة، لا الرحلة."
        inject_spoken_identity(sections, fmt="podcast", closer=closer)
        joined = "\n".join(item["narration"] for item in sections)
        self.assertEqual(joined.count(PRAYER_SENTENCE), 1)
        self.assertEqual(joined.count(PODCAST_CHANNEL_DEFINITION), 0)
        self.assertTrue(sections[-1]["narration"].endswith(closer))
        assert_spoken_identity(sections, fmt="podcast", closer=closer)


class PodcastListenerProxyContractTests(unittest.TestCase):
    def test_listener_question_is_immediately_answered_by_charon_role(self) -> None:
        report = _validate_podcast_listener_proxy_script({
            "sections": [
                {
                    "id": "s1",
                    "narration": "A: لماذا أعرف ما يجب فعله ولا أتحرك؟ B: لأن المعرفة وحدها لا تغيّر نمط الفعل؛ الذي يغيّره هو رد صغير يتكرر في اللحظة نفسها.",
                },
                {
                    "id": "s2",
                    "narration": "A: طيب، فما الذي يتغير أولًا؟ B: يتغير أولًا ردك الصغير في اللحظة نفسها، ثم يصبح هذا الرد أسهل كلما تكرر في السياق نفسه.",
                },
            ]
        })
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["first_speaker"], "A")
        self.assertEqual(report["voices"], {"A": "Orus", "B": "Charon"})

    def test_consecutive_listener_questions_fail_closed(self) -> None:
        with self.assertRaisesRegex(
            RuntimeError,
            "podcast_listener_proxy_hook_requires_immediate_charon_answer",
        ):
            _validate_podcast_listener_proxy_script({
                "sections": [
                    {
                        "id": "s1",
                        "narration": "A: لماذا أعرف ما يجب فعله ولا أتحرك؟ A: وهل المشكلة في الدافع؟ B: ليست المشكلة في المعرفة وحدها.",
                    }
                ]
            })


class PodcastListenerProxyQuestionRescueTests(unittest.TestCase):
    """Run #26: Mistral (the only provider left once gemini/groq/openrouter are
    exhausted) produced a 20-word listener-proxy question against the 18-word
    ceiling, with no alternative provider to retry against. This mirrors the
    Short hook rescue (PR #955) that closed the same failure family for Short.
    """

    _OVERLONG_20_WORD_QUESTION = (
        "طيب لكن كيف نفهم بالضبط لماذا نعود دائمًا إلى نفس القديمة العادة، "
        "رغم أننا نعرف جيدًا تمامًا أنها تؤذينا بشدة؟"
    )
    _UNRESCUABLE_23_WORD_QUESTION = (
        "طيب لكن كيف نفهم بالضبط لماذا نعود دائمًا إلى نفس القديمة السيئة جدًا العادة، "
        "رغم أننا نعرف جيدًا تمامًا أنها تؤذينا بشدة فعلاً؟"
    )
    _S1_ANSWER = (
        "لأن المعرفة وحدها لا تغيّر نمط الفعل؛ الذي يغيّره هو رد صغير يتكرر في اللحظة "
        "نفسها، ويحتاج تكرارًا واعيًا حتى يثبت في السلوك اليومي فعلاً."
    )
    _S2_TURN = (
        "A: طيب، فما الذي يتغير أولًا في هذه الحالة؟ "
        "B: يتغير أولًا ردك الصغير في اللحظة نفسها، ثم يصبح هذا الرد أسهل كلما تكرر "
        "في السياق نفسه، حتى يتحول تدريجيًا إلى عادة جديدة راسخة."
    )

    def test_run26_twenty_word_question_rescued_by_boundary_trim(self) -> None:
        self.assertEqual(len(self._OVERLONG_20_WORD_QUESTION.split()), 20)
        script = {
            "sections": [
                {
                    "id": "s1",
                    "narration": f"A: {self._OVERLONG_20_WORD_QUESTION} B: {self._S1_ANSWER}",
                },
                {"id": "s2", "narration": self._S2_TURN},
            ]
        }

        report = normalize_podcast_listener_proxy_script(script)
        self.assertTrue(report["question_trimmed"])

        trimmed_narration = script["sections"][0]["narration"]
        self.assertNotIn(self._OVERLONG_20_WORD_QUESTION, trimmed_narration)
        self.assertTrue(trimmed_narration.startswith("A: "))
        self.assertIn(self._S1_ANSWER, trimmed_narration)

        trimmed_question = trimmed_narration[len("A: ") : trimmed_narration.index(" B: ")]
        self.assertLessEqual(
            len(trimmed_question.split()), PODCAST_LISTENER_PROXY_QUESTION_MAX_WORDS
        )
        self.assertTrue(trimmed_question.endswith("؟"))

        result = _validate_podcast_listener_proxy_script(script)
        self.assertEqual(result["status"], "pass")

    def test_normalize_leaves_a_compliant_question_untouched(self) -> None:
        script = {
            "sections": [
                {
                    "id": "s1",
                    "narration": f"A: طيب، فما الذي يتغير أولًا؟ B: {self._S1_ANSWER}",
                },
            ]
        }
        original = script["sections"][0]["narration"]

        report = normalize_podcast_listener_proxy_script(script)
        self.assertFalse(report["question_trimmed"])
        self.assertEqual(script["sections"][0]["narration"], original)

    def test_overrun_beyond_rescue_window_is_left_untrimmed_and_fails_closed(self) -> None:
        self.assertEqual(len(self._UNRESCUABLE_23_WORD_QUESTION.split()), 23)
        script = {
            "sections": [
                {
                    "id": "s1",
                    "narration": f"A: {self._UNRESCUABLE_23_WORD_QUESTION} B: {self._S1_ANSWER}",
                },
                {"id": "s2", "narration": self._S2_TURN},
            ]
        }

        report = normalize_podcast_listener_proxy_script(script)
        self.assertFalse(report["question_trimmed"])
        self.assertIn(self._UNRESCUABLE_23_WORD_QUESTION, script["sections"][0]["narration"])

        with self.assertRaisesRegex(
            RuntimeError,
            (
                "podcast_listener_proxy_question_too_long words=23 "
                f"maximum={PODCAST_LISTENER_PROXY_QUESTION_RESCUE_MAX_WORDS}"
            ),
        ):
            _validate_podcast_listener_proxy_script(script)


class PodcastGeminiRoutingTests(unittest.TestCase):
    def test_production_entrypoint_is_gemini_38_only(self) -> None:
        source = Path("clean_v2/__main__.py").read_text(encoding="utf-8")
        self.assertIn("GeminiOnlyVoiceSynthesizer", source)
        self.assertIn("gemini-3.8-flash-tts", source)

    def test_production_workflows_accept_only_gemini_38_voice(self) -> None:
        for workflow in (
            ".github/workflows/clean-v2-minimal-e2e.yml",
            ".github/workflows/clean-v2-podcast-one.yml",
            ".github/workflows/clean-v2-short-final-one.yml",
            ".github/workflows/clean-v2-short-cohort.yml",
            ".github/workflows/clean-v2-telegram-production.yml",
        ):
            source = Path(workflow).read_text(encoding="utf-8")
            self.assertIn("gemini-3.8-flash-tts", source)

    def test_every_production_workflow_resumes_and_caches_exact_tts_chunks(self) -> None:
        for workflow in (
            ".github/workflows/clean-v2-minimal-e2e.yml",
            ".github/workflows/clean-v2-podcast-one.yml",
            ".github/workflows/clean-v2-short-final-one.yml",
            ".github/workflows/clean-v2-short-cohort.yml",
            ".github/workflows/clean-v2-telegram-production.yml",
        ):
            with self.subTest(workflow=workflow):
                source = Path(workflow).read_text(encoding="utf-8")
                self.assertIn("CLEAN_V2_RESUME:", source)
                self.assertIn('CLEAN_V2_TTS_CACHE_PATH:', source)
                self.assertIn('--resume-from "$CLEAN_V2_RESUME', source)
                self.assertIn("scripts/clean_v2_resume_cache.py prepare", source)
                self.assertIn("python -m clean_v2.tts_cache prepare", source)
                self.assertIn("actions/cache/restore@", source)
                self.assertIn("actions/cache/save@", source)

    def test_podcast_uses_gemini_38_only_and_never_falls_back(self) -> None:
        synth = GeminiOnlyVoiceSynthesizer("key")
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "clean_v2.media._gemini38_synthesize",
            side_effect=lambda *_args, **_kwargs: (
                Path(_args[2]).write_bytes(b"G" * 2048) or Path(_args[2])
            ),
        ):
            output = Path(tmp) / "voice.wav"
            synth.synthesize("هذا نص بودكاست عربي.", output)
            self.assertTrue(output.is_file())

        self.assertEqual(synth.last_provider, "gemini-3.8:Charon")
        self.assertFalse(synth.fallback_used)
        self.assertEqual(synth.voice_approval_status, "user_selected_gemini_3_8")

    def test_podcast_gemini_failure_is_terminal_fail_closed(self) -> None:
        synth = GeminiOnlyVoiceSynthesizer("key")
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "clean_v2.media._gemini38_synthesize",
            side_effect=RuntimeError("synthetic Gemini outage"),
        ), mock.patch(
            "clean_v2.media._charon_retry_delay",
            return_value=0.0,
        ):
            with self.assertRaises(VoiceInfrastructureError) as raised:
                synth.synthesize("هذا نص بودكاست عربي.", Path(tmp) / "voice.wav")

        self.assertIn(
            "gemini_3_8_only_fail_closed_no_fallback",
            raised.exception.secondary_reason,
        )
        self.assertFalse(raised.exception.fallback_used)

    def test_dialogue_turns_keep_a_and_b_for_orus_and_charon(self) -> None:
        turns = _gemini38_dialogue_turns(
            "A: لماذا يحدث هذا؟ B: لأننا نخلط بين الخطة والحياة. "
            "A: وما الذي يتغير؟ B: يتغير المقياس الذي نحكم به."
        )
        self.assertEqual(
            turns,
            [
                ("A", "لماذا يحدث هذا؟"),
                ("B", "لأننا نخلط بين الخطة والحياة."),
                ("A", "وما الذي يتغير؟"),
                ("B", "يتغير المقياس الذي نحكم به."),
            ],
        )


class PodcastTelegramTests(unittest.TestCase):
    def test_client_has_one_podcast_choice(self) -> None:
        callbacks = [
            button["callback_data"]
            for row in scope_keyboard()
            for button in row
        ]
        self.assertIn("scope:podcast", callbacks)
        labels = [button["text"] for row in scope_keyboard() for button in row]
        self.assertIn("🎙️ خارج النص", labels)
        instruction = _scope_research_instruction("podcast")
        self.assertIn("سؤال مركزي حقيقي", instruction)
        self.assertIn("يتغير فهم المستمع", instruction)

    def test_materialized_podcast_brief_uses_fixed_gemini_roster_and_allows_dialogue(self) -> None:
        request = {
            "schema_version": 1,
            "request_id": "req-podcast",
            "source": "clean_v2_telegram_editorial_lite",
            "scope": "podcast",
            "approved_by_user": True,
            "approved_topic": "لماذا نعود إلى عادة نعرف أنها تؤذينا؟",
            "research_pack": [],
            "idea_id": "idea-podcast",
            "selected_at": "2026-09-25T00:00:00Z",
            "status": "dispatched",
            "confirmed_at": "2026-09-25T00:01:00Z",
            "dispatched_at": "2026-09-25T00:02:00Z",
        }
        request["request_sha256"] = _request_hash(request)
        state = {"requests": {"req-podcast": request}}
        with tempfile.TemporaryDirectory() as tmp:
            brief = materialize_brief(
                state,
                "req-podcast",
                request["request_sha256"],
                "podcast",
                Path(tmp) / "brief.json",
            )
        self.assertEqual(brief["format"], "podcast")
        self.assertEqual(brief["series_name"], "خارج النص")
        self.assertIn("بصوت القناة الثابت", brief["editorial_intent"])
        self.assertIn("حوارًا حقيقيًا", brief["editorial_intent"])
        self.assertIn("مستمع واحد", brief["editorial_intent"])
        self.assertTrue(any("Gemini 3.8 is the only voice provider" in item for item in brief["hard_constraints"]))
        self.assertTrue(any("Orus" in item and "dialogue_qa" in item for item in brief["hard_constraints"]))
        self.assertTrue(any("simple-deep" in item for item in brief["hard_constraints"]))
        self.assertTrue(any("Arab/Muslim" in item for item in brief["hard_constraints"]))

    def test_podcast_delivery_and_status_keep_the_same_shared_paths(self) -> None:
        root = Path("/tmp/clean-v2-podcast-test")
        self.assertEqual(delivery._target_dirs(root, "podcast"), [("podcast", root / "podcast")])
        self.assertIn("خارج النص", started_text(scope="podcast", topic="موضوع", run_url=""))
        messages = dict(
            milestone_messages(
                {"stages": [{"name": "planning", "status": "pass"}]},
                set(),
                kind="podcast",
            )
        )
        self.assertIn("🎙️ خارج النص", messages["planning"])


class PodcastVisualIdentityTests(unittest.TestCase):
    def test_sparse_key_text_uses_approved_cairo_bold_style_at_most_three_times(self) -> None:
        script = {
            "sections": [
                {"id": "s1", "narration": "أحيانًا نعرف الضرر ونعود إليه. هذه بداية السؤال."},
                {"id": "s2", "narration": "المشكلة أن السلوك القديم يؤدي وظيفة لا نراها. وهنا تتغير الزاوية."},
                {"id": "s3", "narration": "حين نفهم الوظيفة يصبح التغيير أوضح. وهنا تنتهي الفكرة، لا الرحلة."},
            ]
        }
        timeline = {
            "status": "pass",
            "section_events": [
                {"section_id": "s1", "start": 0.0, "end": 10.0},
                {"section_id": "s2", "start": 10.0, "end": 20.0},
                {"section_id": "s3", "start": 20.0, "end": 30.0},
            ],
        }
        events = build_podcast_key_text_events(
            script=script,
            timeline=timeline,
            closer="وهنا تنتهي الفكرة، لا الرحلة.",
        )
        self.assertLessEqual(len(events), 3)
        self.assertEqual([item["role"] for item in events], ["hook", "turn", "payoff"])
        ass = build_podcast_key_text_ass(events)
        self.assertIn("PlayResX: 1920", ass)
        self.assertIn("Style: Caption,Cairo", ass)
        self.assertIn("&H00EEF2F4", ass)
        self.assertNotIn("Style: Shadow", ass)
        self.assertNotIn("Style: Extrusion", ass)
        self.assertNotIn("&H005BA8D7", ass)
        self.assertNotIn("drawbox", ass)

    def test_listener_proxy_key_text_prefers_a_questions_and_hides_labels(self) -> None:
        script = {
            "sections": [
                {
                    "id": "s1",
                    "narration": "A: لماذا أعرف ما يجب فعله ولا أتحرك؟ B: لأن المعرفة وحدها لا تغيّر نمط الفعل.",
                },
                {
                    "id": "s2",
                    "narration": "A: طيب، فما الذي يتغير أولًا؟ B: يتغير أولًا ردك الصغير في اللحظة نفسها.",
                },
                {
                    "id": "s3",
                    "narration": "B: حين يتغير ردك، يبدأ السلوك كله بالتحرك.",
                },
            ]
        }
        timeline = {
            "status": "pass",
            "section_events": [
                {"section_id": "s1", "start": 0.0, "end": 10.0},
                {"section_id": "s2", "start": 10.0, "end": 20.0},
                {"section_id": "s3", "start": 20.0, "end": 30.0},
            ],
        }
        events = build_podcast_key_text_events(script=script, timeline=timeline)
        self.assertEqual(events[0]["text"], "لماذا أعرف ما يجب فعله ولا أتحرك؟")
        self.assertEqual(events[1]["text"], "طيب، فما الذي يتغير أولًا؟")
        self.assertEqual(events[-1]["text"], "حين يتغير ردك، يبدأ السلوك كله بالتحرك.")
        ass = build_podcast_key_text_ass(events)
        self.assertNotIn("A:", ass)
        self.assertNotIn("B:", ass)

    def test_local_3d_render_failure_is_wrapped_for_pipeline_fail_soft(self) -> None:
        script = {
            "sections": [
                {"id": "s1", "narration": "هذه بداية الفكرة."},
                {"id": "s2", "narration": "وهنا تتغير الزاوية."},
            ]
        }
        timeline = {
            "status": "pass",
            "section_events": [
                {"section_id": "s1", "start": 0.0, "end": 8.0},
                {"section_id": "s2", "start": 8.0, "end": 16.0},
            ],
            "identity_events": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "timeline-first.json").write_text(
                __import__("json").dumps(timeline, ensure_ascii=False),
                encoding="utf-8",
            )
            final_path = root / "final.mp4"
            final_path.write_bytes(b"video")
            with mock.patch(
                "clean_v2.podcast_key_text.subprocess.run",
                side_effect=__import__("subprocess").CalledProcessError(1, ["ffmpeg"]),
            ):
                with self.assertRaisesRegex(
                    PodcastKeyTextError,
                    "podcast_key_text_local_render_failed",
                ):
                    apply_podcast_key_text(
                        output_dir=root,
                        final_path=final_path,
                        script=script,
                    )
            self.assertEqual(final_path.read_bytes(), b"video")

    def test_cultural_islamic_visual_risk_is_a_local_fail_closed_gate(self) -> None:
        safe = _apply_cultural_islamic_policy(
            {
                "status": "pass",
                "cultural_conflict": False,
                "cultural_islamic_suitability_risk": False,
                "reason": "safe",
            }
        )
        self.assertEqual(safe["status"], "pass")
        self.assertEqual(safe["cultural_islamic_policy"], "pass")

        risky = _apply_cultural_islamic_policy(
            {
                "status": "pass",
                "cultural_conflict": False,
                "cultural_islamic_suitability_risk": True,
                "reason": "revealing clothing",
            }
        )
        self.assertEqual(risky["status"], "block")
        self.assertEqual(risky["cultural_islamic_policy"], "block")

        missing = _apply_cultural_islamic_policy({"status": "pass", "reason": "unknown"})
        self.assertEqual(missing["status"], "pass")
        self.assertEqual(
            missing["cultural_islamic_policy"],
            "advisory_missing_evidence",
        )


class PodcastDerivedShortLiteTests(unittest.TestCase):
    def test_pipeline_wires_selected_podcast_promo_into_voice_timeline(self) -> None:
        source = inspect.getsource(CleanV2Pipeline.run)
        self.assertIn("podcast_promo=podcast_promo", source)
        self.assertIn("recent_signatures=podcast_promo_history", source)
        self.assertIn("record_derived_short_signature(", source)

    def test_local_promo_selection_avoids_opening_and_preserves_text(self) -> None:
        sections = [
            {
                "id": "s1",
                "narration": "هذا هو الهوك. اللهم صل وسلم على نبينا محمد. تعريف القناة ثم نبدأ.",
            },
            {
                "id": "s2",
                "narration": (
                    "نحن لا نعود إلى العادة القديمة لأننا نسينا ضررها. "
                    "المشكلة أنها ما زالت تؤدي وظيفة يومية لا نعرف كيف نستبدلها. "
                    "ولهذا يصبح القرار وحده أضعف من البيئة التي تعيد السلوك كل مرة."
                ),
            },
        ]
        promo = _select_podcast_promo_excerpt(sections)
        self.assertIsNotNone(promo)
        assert promo is not None
        self.assertEqual(promo["section_id"], "s2")
        self.assertNotIn("هذا هو الهوك", promo["text"])

        original = [
            ("topic", "قبل المقطع."),
            ("topic", promo["text"]),
            ("topic", "بعد المقطع."),
        ]
        isolated = _isolate_podcast_promo_unit(original, promo["text"])
        self.assertEqual([role for role, _ in isolated].count("promo_short"), 1)
        before = " ".join(text for _role, text in original)
        after = " ".join(text for _role, text in isolated)
        self.assertEqual(" ".join(before.split()), " ".join(after.split()))

    def test_promo_selection_avoids_recent_pattern_when_an_alternative_exists(self) -> None:
        sections = [
            {"id": "s1", "narration": "A: لماذا نكرر السلوك نفسه؟ B: لأن القرار وحده لا يغير البيئة."},
            {
                "id": "s2",
                "narration": (
                    "المشكلة أن السلوك القديم يختصر عليك قرارًا يوميًا مرهقًا في اللحظة نفسها. "
                    "ولهذا يبدو الرجوع إليه أسهل حتى عندما تعرف أن نتيجته لا تناسبك على المدى الطويل. "
                    "حين تغيّر الإشارة التي تسبق السلوك، يصبح أمامك مسار آخر قبل أن تبدأ العادة تلقائيًا."
                ),
            },
            {
                "id": "s3",
                "narration": (
                    "لكن الإرادة وحدها لا تكفي إذا بقيت كل التفاصيل حولك تدفعك إلى المسار القديم. "
                    "بل تحتاج أن تجعل البديل الأقرب واضحًا وسهلًا قبل لحظة الاختيار التي تتكرر كل يوم. "
                    "الحقيقة أن التغيير يصبح أهدأ عندما تقلل عدد القرارات التي تتخذها تحت الضغط."
                ),
            },
        ]
        first = _select_podcast_promo_excerpt(sections)
        self.assertIsNotNone(first)
        assert first is not None
        second = _select_podcast_promo_excerpt(
            sections,
            recent_signatures=(first["selection_signature"],),
        )
        self.assertIsNotNone(second)
        assert second is not None
        self.assertNotEqual(second["selection_signature"], first["selection_signature"])
        self.assertNotEqual(second["text"], first["text"])
        self.assertEqual(second["selection_basis"], "local_quality_score_v2_history_aware")

    def test_derived_short_reuses_final_and_passes_existing_qc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            final_path = root / "final.mp4"
            final_path.write_bytes(b"video-source")
            (root / "timeline-first.json").write_text(
                __import__("json").dumps(
                    {
                        "audio_units": [
                            {
                                "role": "promo_short",
                                "start": 12.0,
                                "end": 27.0,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            def fake_render(_source, output, **_kwargs):
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(b"derived-video")
                return output

            with mock.patch(
                "clean_v2.pipeline.render_derived_short",
                side_effect=fake_render,
            ):
                report = _run_podcast_derived_short_lite(
                    output_dir=root,
                    final_path=final_path,
                    final_master_qc=lambda _root: {"status": "pass"},
                )
            self.assertEqual(report["status"], "pass")
            self.assertTrue((root / "podcast-short.mp4").is_file())
            self.assertTrue((root / "podcast-short-qc.json").is_file())

    def test_derived_short_qc_failure_never_fails_episode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            final_path = root / "final.mp4"
            final_path.write_bytes(b"video-source")
            (root / "timeline-first.json").write_text(
                __import__("json").dumps(
                    {
                        "audio_units": [
                            {
                                "role": "promo_short",
                                "start": 5.0,
                                "end": 20.0,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            def fake_render(_source, output, **_kwargs):
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(b"derived-video")
                return output

            with mock.patch(
                "clean_v2.pipeline.render_derived_short",
                side_effect=fake_render,
            ):
                report = _run_podcast_derived_short_lite(
                    output_dir=root,
                    final_path=final_path,
                    final_master_qc=lambda _root: (_ for _ in ()).throw(
                        RuntimeError("qc blocked")
                    ),
                )
            self.assertEqual(report["status"], "skipped_failed")
            self.assertTrue(final_path.is_file())
            self.assertFalse((root / "podcast-short.mp4").exists())


if __name__ == "__main__":
    unittest.main()

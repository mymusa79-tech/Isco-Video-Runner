from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clean_v2.contracts import ContractError, SUPPORTED_FORMATS, validate_plan
from clean_v2.identity_sequence import (
    LONG_CHANNEL_DEFINITION,
    PRAYER_SENTENCE,
    assert_spoken_identity,
    inject_spoken_identity,
)
from clean_v2.media import GeminiPrimaryNabraFallbackSynthesizer
from clean_v2.pipeline import (
    CleanV2Pipeline,
    _isolate_podcast_promo_unit,
    _planning_prompt,
    _run_podcast_derived_short_lite,
    _script_prompt,
    _select_podcast_promo_excerpt,
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


class _FakeNabra:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def synthesize(self, transcript: str, output_path: Path) -> Path:
        self.calls.append(transcript)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"RIFF" + b"0" * 2048)
        return output_path


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
        self.assertNotIn("payoff_answer must be a descriptive resolution", planning)
        self.assertIn("neutral female narrator", script)
        self.assertIn("local Nabra af_msa", script)
        self.assertIn("Never invent first-person", script)
        self.assertIn("Do not write toward a word-count or duration target", script)
        self.assertIn("audio alone", script)
        self.assertIn("article, lecture, news script", script)
        self.assertIn("speaking simply to one listener", script)
        self.assertIn("numbered-list", script)
        self.assertNotIn("HARD maximum of 18 Arabic words", script)

    def test_podcast_reuses_long_identity_without_a_new_identity_system(self) -> None:
        sections = [
            {"id": "s1", "narration": "لماذا نعود إلى ما قررنا تركه؟ نبدأ من وظيفة السلوك نفسه."},
            {"id": "s2", "narration": "عندما نفهم الوظيفة يصبح التغيير أوضح."},
        ]
        closer = "وهنا تنتهي الفكرة، لا الرحلة."
        inject_spoken_identity(sections, fmt="podcast", closer=closer)
        joined = "\n".join(item["narration"] for item in sections)
        self.assertEqual(joined.count(PRAYER_SENTENCE), 1)
        self.assertEqual(joined.count(LONG_CHANNEL_DEFINITION), 1)
        self.assertTrue(sections[-1]["narration"].endswith(closer))
        assert_spoken_identity(sections, fmt="podcast", closer=closer)


class PodcastNabraRoutingTests(unittest.TestCase):
    def test_podcast_primary_lock_never_attempts_charon_and_is_not_fallback(self) -> None:
        fake = _FakeNabra()
        synth = GeminiPrimaryNabraFallbackSynthesizer("", nabra=fake)
        synth.activate_full_run_nabra_primary()
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "clean_v2.media._legacy_voice_identity", return_value=("Charon", "Orus")
        ), mock.patch(
            "clean_v2.media._legacy_gemini_synthesize",
            side_effect=AssertionError("Charon must not be called for Podcast"),
        ):
            result = synth.synthesize("هذا نص بودكاست عربي.", Path(tmp) / "voice.wav")
            self.assertTrue(result.is_file())
        self.assertEqual(fake.calls, ["هذا نص بودكاست عربي."])
        self.assertEqual(synth.last_provider, "nabra:af_msa")
        self.assertFalse(synth.fallback_used)
        self.assertEqual(synth.charon_attempts, 0)
        self.assertEqual(synth.voice_approval_status, "user_selected_primary")


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

    def test_materialized_podcast_brief_keeps_choice_simple_and_marks_female_narration(self) -> None:
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
        self.assertIn("راوية أنثوية محايدة", brief["editorial_intent"])
        self.assertIn("مستمع واحد", brief["editorial_intent"])
        self.assertTrue(any("female narrator" in item for item in brief["hard_constraints"]))
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
    def test_sparse_key_text_uses_approved_3d_style_at_most_three_times(self) -> None:
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
        self.assertIn("Style: Shadow", ass)
        self.assertIn("Style: Extrusion", ass)
        self.assertIn("&H005BA8D7", ass)
        self.assertNotIn("drawbox", ass)

    def test_film_key_text_is_sparse_large_and_separated_by_clean_gaps(self) -> None:
        script = {
            "sections": [
                {"id": "s1", "narration": "ابدأ قبل أن تصبح الخطة عبئًا. هذه بداية التغيير."},
                {"id": "s2", "narration": "المشكلة أن كثرة الخيارات تستهلك الانتباه. خطوة واحدة تكفي."},
                {"id": "s3", "narration": "وهنا يظهر الفرق الحقيقي. الوضوح يسبق السرعة دائمًا."},
                {"id": "s4", "narration": "لهذا يصبح التنفيذ أبسط. القرار الصغير يفتح الطريق."},
                {"id": "s5", "narration": "في النهاية لا تحتاج خطة مثالية. تحتاج بداية قابلة للاستمرار."},
            ]
        }
        timeline = {
            "status": "pass",
            "section_events": [
                {"section_id": "s1", "start": 0.0, "end": 60.0},
                {"section_id": "s2", "start": 60.0, "end": 120.0},
                {"section_id": "s3", "start": 120.0, "end": 180.0},
                {"section_id": "s4", "start": 180.0, "end": 240.0},
                {"section_id": "s5", "start": 240.0, "end": 300.0},
            ],
            "identity_events": [
                {"kind": "topic", "start": 20.0, "end": 294.0},
                {"kind": "outro", "start": 294.0, "end": 299.0},
            ],
        }
        events = build_podcast_key_text_events(
            script=script,
            timeline=timeline,
            fmt="film",
        )
        self.assertGreaterEqual(len(events), 3)
        self.assertLessEqual(len(events), 5)
        for item in events:
            self.assertLessEqual(len(str(item["text"]).split()), 10)
            self.assertLessEqual(float(item["end"]) - float(item["start"]), 5.0)
        for previous, current in zip(events, events[1:]):
            self.assertGreaterEqual(float(current["start"]) - float(previous["end"]), 12.0)
        ass = build_podcast_key_text_ass(events, fmt="film")
        self.assertIn("PlayResX: 1920", ass)
        self.assertIn(r"\fscx99\fscy99", ass)
        self.assertNotIn("drawbox", ass)
        self.assertNotIn(r"\bord5", ass)

    def test_sparse_key_text_never_splits_sentence_at_arabic_comma(self) -> None:
        script = {
            "sections": [
                {"id": "s1", "narration": "ابدأ بخطوة صغيرة، لأن الاستمرار أهم من الكمال."},
                {"id": "s2", "narration": "الوضوح يصنع الفرق."},
                {"id": "s3", "narration": "وهنا تصل الفكرة إلى نهايتها."},
            ]
        }
        timeline = {
            "status": "pass",
            "section_events": [
                {"section_id": "s1", "start": 0.0, "end": 40.0},
                {"section_id": "s2", "start": 40.0, "end": 80.0},
                {"section_id": "s3", "start": 80.0, "end": 120.0},
            ],
            "identity_events": [],
        }
        events = build_podcast_key_text_events(
            script=script,
            timeline=timeline,
            fmt="film",
        )
        texts = [str(item["text"]) for item in events]
        self.assertIn("ابدأ بخطوة صغيرة، لأن الاستمرار أهم من الكمال.", texts)
        self.assertNotIn("ابدأ بخطوة صغيرة", texts)
        ass = build_podcast_key_text_ass(events, fmt="film")
        self.assertIn(r"\h", ass)
        self.assertNotIn(r"\bord5", ass)

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

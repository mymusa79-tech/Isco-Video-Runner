from __future__ import annotations

import base64
import io
import json
import os
import tempfile
import unittest
import urllib.error
import wave
from pathlib import Path
from unittest.mock import patch

from clean_v2.media import (
    GEMINI38_INNER_REFLECTIVE_STYLE,
    GEMINI38_INNER_RESOLVED_STYLE,
    GEMINI38_LITE_PROVIDER,
    GEMINI38_LITE_TTS_MODEL,
    GEMINI38_NARRATOR_STYLE,
    GEMINI38_PROVIDER,
    GEMINI38_QUESTIONER_STYLE,
    GEMINI38_TTS_MODEL,
    GeminiOnlyVoiceSynthesizer,
    TtsProviderError,
    VoiceInfrastructureError,
    _gemini38_synthesize,
    _spoken_voice_roles,
)
from clean_v2.pipeline import (
    STAGES,
    _Journal,
    _bounded_voice_chunks,
    _isolate_topic_phrase_unit,
    _reattach_dialogue_speaker_label,
    _synthesize_sectioned_voice,
)


def _write_audio(path: Path, marker: bytes = b"G") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(marker * 2048)
    return path


def _wav_bytes() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(b"\x00\x00" * 4096)
    return buffer.getvalue()


class _RateLimit(RuntimeError):
    http_status = 429
    retry_after_seconds = 0.25


class _LongRateLimit(RuntimeError):
    http_status = 429
    retry_after_seconds = 30.0


class _FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, _limit: int = -1) -> bytes:
        return self.payload


class CleanV2Gemini38VoiceTests(unittest.TestCase):
    def test_turn_metadata_styles_are_short_stable_and_persona_free(self) -> None:
        self.assertIn("Natural Modern Standard Arabic", GEMINI38_NARRATOR_STYLE)
        self.assertIn("conversational", GEMINI38_NARRATOR_STYLE)
        self.assertIn("human pacing", GEMINI38_NARRATOR_STYLE)
        self.assertIn("no announcer tone", GEMINI38_NARRATOR_STYLE)
        self.assertIn("firm but calm", GEMINI38_QUESTIONER_STYLE)
        self.assertIn("no second character", GEMINI38_INNER_REFLECTIVE_STYLE)
        self.assertIn("Same exact Charon speaker and identity", GEMINI38_INNER_RESOLVED_STYLE)

    def test_success_uses_only_gemini38_charon(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            synth = GeminiOnlyVoiceSynthesizer("gemini-test-key")
            with patch(
                "clean_v2.media._gemini38_synthesize",
                side_effect=lambda _key, _text, path, **_kwargs: _write_audio(Path(path)),
            ) as tts:
                result = synth.synthesize(
                    "هذا صوت القناة الرئيسي.",
                    root / "narration.wav",
                )

        self.assertEqual(result.name, "narration.wav")
        self.assertEqual(tts.call_count, 1)
        self.assertEqual(synth.last_provider, GEMINI38_PROVIDER)
        self.assertFalse(synth.fallback_used)
        self.assertEqual(synth.charon_attempts, 1)
        self.assertEqual(synth.voice_roles, {"mode": "single_narrator", "narrator": "Charon"})
        self.assertEqual(synth.voice_approval_status, "user_selected_gemini_3_8")

    def test_short_retry_after_retries_same_gemini_provider(self) -> None:
        calls = {"count": 0}

        def tts(_key, _text, path, **_kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                raise _RateLimit("http_429")
            return _write_audio(Path(path))

        with tempfile.TemporaryDirectory() as temporary:
            synth = GeminiOnlyVoiceSynthesizer("gemini-test-key")
            with patch("clean_v2.media._gemini38_synthesize", side_effect=tts), patch(
                "clean_v2.media.time.sleep"
            ) as sleep:
                synth.synthesize("نص قصير.", Path(temporary) / "out.wav")

        self.assertEqual(calls["count"], 2)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.25])
        self.assertFalse(synth.fallback_used)

    def test_long_retry_after_fails_closed_without_substitution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            synth = GeminiOnlyVoiceSynthesizer("gemini-test-key")
            with patch(
                "clean_v2.media._gemini38_synthesize",
                side_effect=_LongRateLimit("http_429"),
            ) as tts, patch("clean_v2.media.time.sleep") as sleep:
                with self.assertRaises(VoiceInfrastructureError) as raised:
                    synth.synthesize("نص قصير.", Path(temporary) / "out.wav")

        # A quota error with no usable short retry delay is terminal for this
        # run: no model substitution and no repeated quota spend.
        self.assertEqual(tts.call_count, 1)
        sleep.assert_not_called()
        self.assertFalse(raised.exception.fallback_used)

    def test_plain_quota_text_is_not_retried(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            synth = GeminiOnlyVoiceSynthesizer("gemini-test-key")
            with patch(
                "clean_v2.media._gemini38_synthesize",
                side_effect=RuntimeError("429 RESOURCE_EXHAUSTED quota exceeded"),
            ) as tts, patch("clean_v2.media.time.sleep") as sleep:
                with self.assertRaises(VoiceInfrastructureError):
                    synth.synthesize("نص قصير.", Path(temporary) / "out.wav")

        self.assertEqual(tts.call_count, 1)
        sleep.assert_not_called()

    def test_persistent_gemini_failure_is_terminal_after_bounded_retries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            synth = GeminiOnlyVoiceSynthesizer("gemini-test-key")
            with patch(
                "clean_v2.media._gemini38_synthesize",
                side_effect=RuntimeError("temporary provider failure"),
            ) as tts, patch("clean_v2.media.time.sleep"):
                with self.assertRaises(VoiceInfrastructureError) as raised:
                    synth.synthesize("نص قصير.", Path(temporary) / "out.wav")

        self.assertEqual(tts.call_count, 2)
        self.assertEqual(raised.exception.charon_attempts, 2)
        self.assertFalse(raised.exception.fallback_used)
        self.assertIn("fallback=false", str(raised.exception))

    def test_synthesize_uses_algenib_and_reports_fallback_used_on_lite_model(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            synth = GeminiOnlyVoiceSynthesizer(
                "gemini-test-key", tts_model=GEMINI38_LITE_TTS_MODEL
            )
            with patch(
                "clean_v2.media._gemini38_synthesize",
                side_effect=lambda _key, _text, path, **_kwargs: _write_audio(Path(path)),
            ) as tts:
                synth.synthesize("نص قصير.", Path(temporary) / "out.wav")

        # Reviewed by ear: Algenib reads better than lite-Charon in Arabic on
        # the explicitly configured lite route, so it uses Algenib as the
        # primary voice
        # while Orus stays the fixed questioner - and the reported provider
        # identity reflects that voice honestly instead of claiming Charon.
        self.assertEqual(tts.call_args.kwargs.get("model"), GEMINI38_LITE_TTS_MODEL)
        self.assertEqual(tts.call_args.kwargs.get("primary_voice"), "Algenib")
        self.assertEqual(tts.call_args.kwargs.get("questioner_voice"), "Orus")
        self.assertEqual(synth.last_provider, GEMINI38_LITE_PROVIDER)
        self.assertTrue(synth.fallback_used)

    def test_model_drift_is_rejected_before_provider_call(self) -> None:
        synth = GeminiOnlyVoiceSynthesizer(
            "gemini-test-key",
            tts_model="gemini-3.1-flash-tts-preview",
        )
        with tempfile.TemporaryDirectory() as temporary, patch(
            "clean_v2.media._gemini38_synthesize"
        ) as tts:
            with self.assertRaisesRegex(RuntimeError, "model drift"):
                synth.synthesize("نص قصير.", Path(temporary) / "out.wav")
        tts.assert_not_called()

    def test_inner_dialogue_remains_one_charon_voice(self) -> None:
        transcript = (
            "لماذا أؤجلها مرة أخرى؟ هل أنا كسول؟ "
            "لا. ربما أنا أنتظر أن أشعر بالاستعداد قبل أن أبدأ."
        )
        self.assertEqual(
            _spoken_voice_roles(transcript),
            {"mode": "single_narrator", "narrator": "Charon"},
        )

    def test_dialogue_qa_maps_a_to_orus_and_b_to_charon(self) -> None:
        transcript = "A: لماذا يحدث هذا؟\nB: لأن الخطة تفترض يومًا مثاليًا."
        self.assertEqual(
            _spoken_voice_roles(transcript),
            {
                "mode": "dialogue_qa",
                "questioner": "Orus",
                "responder": "Charon",
            },
        )

    def test_inner_dialogue_payload_keeps_one_charon_and_approved_progression(self) -> None:
        captured: dict[str, object] = {}
        audio = base64.b64encode(_wav_bytes()).decode("ascii")
        response = json.dumps(
            {"steps": [{"content": [{"type": "audio", "data": audio}]}]}
        ).encode("utf-8")

        def urlopen(request, timeout):
            captured["request"] = request
            return _FakeResponse(response)

        transcript = (
            "لماذا أؤجلها مرة أخرى؟ هل المشكلة فعلًا أنني كسول؟ "
            "لا. ربما أنا أنتظر أن أشعر بالاستعداد قبل أن أبدأ."
        )
        with tempfile.TemporaryDirectory() as temporary, patch(
            "clean_v2.media.urllib.request.urlopen", side_effect=urlopen
        ):
            _gemini38_synthesize(
                "gemini-test-key",
                transcript,
                Path(temporary) / "inner.wav",
                model=GEMINI38_TTS_MODEL,
                primary_voice="Charon",
                questioner_voice="Orus",
                performance_mode="inner_dialogue",
            )

        payload = json.loads(captured["request"].data.decode("utf-8"))
        self.assertEqual(payload["generation_config"]["speech_config"], [{"voice": "Charon"}])
        parts = payload["input"][0]["content"]
        self.assertGreaterEqual(len(parts), 2)
        self.assertTrue(all("speaker" not in part["annotations"][0] for part in parts))
        self.assertTrue(all(
            part["annotations"][0]["style"] == GEMINI38_INNER_REFLECTIVE_STYLE
            for part in parts[:-1]
        ))
        self.assertEqual(
            parts[-1]["annotations"][0]["style"],
            GEMINI38_INNER_RESOLVED_STYLE,
        )

    def test_gemini38_rest_single_voice_payload_uses_charon_without_speaker_metadata(self) -> None:
        captured: dict[str, object] = {}
        audio = base64.b64encode(_wav_bytes()).decode("ascii")
        response = json.dumps(
            {"steps": [{"content": [{"type": "audio", "data": audio}]}]}
        ).encode("utf-8")

        def urlopen(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return _FakeResponse(response)

        with tempfile.TemporaryDirectory() as temporary, patch(
            "clean_v2.media.urllib.request.urlopen", side_effect=urlopen
        ):
            target = _gemini38_synthesize(
                "gemini-test-key",
                "هذا حوار داخلي بصوت واحد.",
                Path(temporary) / "out.wav",
                model=GEMINI38_TTS_MODEL,
                primary_voice="Charon",
                questioner_voice="Orus",
            )
            self.assertTrue(target.is_file())

        payload = json.loads(captured["request"].data.decode("utf-8"))
        self.assertEqual(payload["model"], GEMINI38_TTS_MODEL)
        self.assertEqual(payload["generation_config"]["speech_config"], [{"voice": "Charon"}])
        annotations = payload["input"][0]["content"][0]["annotations"][0]
        self.assertNotIn("speaker", annotations)

    def test_gemini38_rest_dialogue_payload_uses_orus_and_charon(self) -> None:
        captured: dict[str, object] = {}
        audio = base64.b64encode(_wav_bytes()).decode("ascii")
        response = json.dumps(
            {"steps": [{"content": [{"type": "audio", "data": audio}]}]}
        ).encode("utf-8")

        def urlopen(request, timeout):
            captured["request"] = request
            return _FakeResponse(response)

        with tempfile.TemporaryDirectory() as temporary, patch(
            "clean_v2.media.urllib.request.urlopen", side_effect=urlopen
        ):
            _gemini38_synthesize(
                "gemini-test-key",
                "A: لماذا يحدث هذا؟\nB: لأن الخطة تفترض يومًا مثاليًا.",
                Path(temporary) / "dialogue.wav",
                model=GEMINI38_TTS_MODEL,
                primary_voice="Charon",
                questioner_voice="Orus",
            )

        payload = json.loads(captured["request"].data.decode("utf-8"))
        speech = payload["generation_config"]["speech_config"]
        self.assertEqual(speech["mode"], "conversational")
        self.assertEqual(
            speech["speakers"],
            [
                {"speaker": "A", "voice": "Orus"},
                {"speaker": "B", "voice": "Charon"},
            ],
        )
        turns = payload["input"][0]["content"]
        self.assertEqual([turn["annotations"][0]["speaker"] for turn in turns], ["A", "B"])
        self.assertEqual([turn["text"] for turn in turns], [
            "لماذا يحدث هذا؟",
            "لأن الخطة تفترض يومًا مثاليًا.",
        ])

    def test_gemini_http_error_preserves_retry_metadata(self) -> None:
        body = json.dumps(
            {
                "error": {
                    "code": 429,
                    "message": "Resource exhausted. Please retry in 3720.25s.",
                    "details": [
                        {
                            "@type": "type.googleapis.com/google.rpc.RetryInfo",
                            "retryDelay": "3720.25s",
                        }
                    ],
                }
            }
        ).encode("utf-8")
        error = urllib.error.HTTPError(
            "https://example.invalid",
            429,
            "Too Many Requests",
            {},
            io.BytesIO(body),
        )

        with tempfile.TemporaryDirectory() as temporary, patch(
            "clean_v2.media.urllib.request.urlopen",
            side_effect=error,
        ):
            with self.assertRaises(TtsProviderError) as raised:
                _gemini38_synthesize(
                    "gemini-test-key",
                    "اختبار.",
                    Path(temporary) / "out.wav",
                    model=GEMINI38_TTS_MODEL,
                    primary_voice="Charon",
                    questioner_voice="Orus",
                )

        self.assertEqual(raised.exception.http_status, 429)
        self.assertAlmostEqual(raised.exception.retry_after_seconds or 0.0, 3720.25)

    def test_cta_phrase_isolated_only_from_topic_voice_units(self) -> None:
        cta = "إذا أضافت لك الفكرة شيئًا، يكفيني إعجابك."
        units = [
            ("hook", "لماذا نتردد؟"),
            ("prayer", "اللهم صلِّ وسلِّم على نبينا محمد."),
            ("channel_identity", "هنا نداء اليقظة."),
            (
                "topic",
                "كل مقارنة إضافية تستهلك انتباهك. "
                + cta
                + " ثم نعود إلى الفكرة الأساسية."
            ),
            ("outro", "نلتقي في نداء جديد."),
        ]
        isolated = _isolate_topic_phrase_unit(
            units,
            cta,
            role_name="cta_topic",
        )
        self.assertEqual(
            [role for role, _text in isolated].count("cta_topic"),
            1,
        )
        self.assertEqual(
            next(text for role, text in isolated if role == "cta_topic"),
            cta,
        )
        self.assertEqual(isolated[0][0], "hook")
        self.assertEqual(isolated[1][0], "prayer")
        self.assertEqual(isolated[2][0], "channel_identity")
        self.assertEqual(isolated[-1][0], "outro")

    def test_sectioned_voice_retries_only_failed_section_and_stays_gemini38(self) -> None:
        calls: list[str] = []
        failures = {"s2": 0}

        def tts(_key, transcript, path, **_kwargs):
            calls.append(transcript)
            if transcript == "القسم الثاني." and failures["s2"] < 1:
                failures["s2"] += 1
                raise RuntimeError("temporary")
            return _write_audio(Path(path))

        def concat_audio(inputs, target):
            self.assertEqual([path.name for path in inputs], ["01.wav", "02.wav", "03.wav"])
            return _write_audio(Path(target), b"J")

        sections = [
            {"id": "s1", "narration": "القسم الأول."},
            {"id": "s2", "narration": "القسم الثاني."},
            {"id": "s3", "narration": "القسم الثالث."},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            synth = GeminiOnlyVoiceSynthesizer("gemini-test-key")
            with patch("clean_v2.media._gemini38_synthesize", side_effect=tts), patch(
                "clean_v2.media.time.sleep"
            ), patch(
                "clean_v2.pipeline.concat_wav_parts", side_effect=concat_audio
            ):
                report = _synthesize_sectioned_voice(
                    synth,
                    sections,
                    root / "narration.wav",
                    require_charon_only=True,
                )

        self.assertEqual(
            calls,
            [
                "القسم الأول.",
                "القسم الثاني.",
                "القسم الثاني.",
                "القسم الثالث.",
            ],
        )
        self.assertEqual(report["voice_provider"], GEMINI38_PROVIDER)
        self.assertFalse(report["voice_fallback_used"])

    def test_sectioned_voice_preserves_partial_output_without_lite_restart(
        self,
    ) -> None:
        calls: list[tuple[str, str]] = []

        def tts(_key, transcript, path, *, model, **_kwargs):
            calls.append((transcript, model))
            if model == GEMINI38_TTS_MODEL and transcript == "القسم الثاني.":
                raise TtsProviderError(
                    "gemini_3_8_http_429",
                    http_status=429,
                    retry_after_seconds=3661.0,
                )
            return _write_audio(Path(path))

        sections = [
            {"id": "s1", "narration": "القسم الأول."},
            {"id": "s2", "narration": "القسم الثاني."},
        ]

        def concat_audio(inputs, target):
            return _write_audio(Path(target), b"J")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            synth = GeminiOnlyVoiceSynthesizer("gemini-test-key")
            with patch("clean_v2.media._gemini38_synthesize", side_effect=tts), patch(
                "clean_v2.media.time.sleep"
            ), patch(
                "clean_v2.pipeline.concat_wav_parts", side_effect=concat_audio
            ):
                with self.assertRaises(VoiceInfrastructureError) as raised:
                    _synthesize_sectioned_voice(
                        synth,
                        sections,
                        root / "narration.wav",
                        require_charon_only=True,
                    )

            report = json.loads(
                (root / "voice-sections.json").read_text(encoding="utf-8")
            )
            first_section_preserved = (root / "audio" / "01.wav").is_file()

        # s1 succeeds once and remains available for the exact-content cache;
        # the quota failure on s2 gets one wire call and never starts a second
        # whole narration on the lite model.
        self.assertEqual(
            [item for item in calls if item[1] == GEMINI38_TTS_MODEL],
            [("القسم الأول.", GEMINI38_TTS_MODEL), ("القسم الثاني.", GEMINI38_TTS_MODEL)],
        )
        self.assertEqual(
            [item for item in calls if item[1] == GEMINI38_LITE_TTS_MODEL], []
        )
        self.assertTrue(first_section_preserved)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["failed_section"], "s2")
        self.assertEqual(report["tts_wire_attempts"], 2)
        self.assertEqual(report["tts_cache_hits"], 0)
        self.assertEqual(report["retry_after_seconds"], 3661.0)
        self.assertEqual(raised.exception.retry_after_seconds, 3661.0)

    def test_exact_tts_chunk_is_restored_without_provider_call(self) -> None:
        calls = {"count": 0}

        def tts(_key, _text, path, **_kwargs):
            calls["count"] += 1
            Path(path).write_bytes(_wav_bytes())
            return Path(path)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache_root = root / "cache"
            with patch.dict(
                os.environ,
                {"CLEAN_V2_TTS_CACHE_PATH": str(cache_root)},
                clear=False,
            ), patch("clean_v2.media._gemini38_synthesize", side_effect=tts):
                first = GeminiOnlyVoiceSynthesizer("gemini-test-key")
                first.synthesize("نص مطابق قابل للاستئناف.", root / "first.wav")
                second = GeminiOnlyVoiceSynthesizer("")
                second.synthesize("نص مطابق قابل للاستئناف.", root / "second.wav")

            self.assertEqual(calls["count"], 1)
            self.assertFalse(first.cache_hit)
            self.assertTrue(second.cache_hit)
            self.assertEqual(second.charon_attempts, 0)
            self.assertEqual(
                (root / "first.wav").read_bytes(),
                (root / "second.wav").read_bytes(),
            )

    def test_dialogue_chunking_preserves_complete_turns(self) -> None:
        source = (
            "A: لماذا نفشل رغم أن الخطة جيدة؟\n"
            "B: لأن الخطة قد تفترض يومًا مثاليًا لا يشبه حياتنا.\n"
            "A: إذن ماذا نغيّر؟\n"
            "B: نغيّر الخطة كي تتحمل اليوم الحقيقي."
        )
        chunks = _bounded_voice_chunks(source, max_chars=120)
        self.assertTrue(chunks)
        self.assertEqual(" ".join(" ".join(chunks).split()), " ".join(source.split()))
        for chunk in chunks:
            for line in chunk.splitlines():
                self.assertRegex(line, r"^[AB]:\s*\S")

    def test_reattach_dialogue_speaker_label_keeps_open_turn_labelled(self) -> None:
        # Reproduces the live production failure (podcast Run #25): the prayer
        # sentence sits inside speaker A's opening turn, not between turns, so
        # slicing at the prayer boundary strips A's label from the turn's
        # continuation ("حتى بعد أن نقرر التوقف عنها..."). Left unlabelled and
        # then followed by a real "B:" turn, _bounded_voice_chunks correctly
        # rejects the mix as inconsistent dialogue.
        hook = "A: لماذا نعود إلى تلك العادة التي نعرف أنها تؤذينا؟"
        continuation = (
            "حتى بعد أن نقرر التوقف عنها، لماذا نعود؟ "
            "B: لأن تلك العادة لم تكن مجرد اختيار، بل كانت حلًا غير مرئي لمشكلة لم نلاحظها."
        )
        relabelled = _reattach_dialogue_speaker_label(hook, continuation)
        self.assertTrue(relabelled.startswith("A: "))
        chunks = _bounded_voice_chunks(relabelled, max_chars=400)
        for chunk in chunks:
            for line in chunk.splitlines():
                self.assertRegex(line, r"^[AB]:\s*\S")

    def test_reattach_dialogue_speaker_label_is_noop_when_already_labelled(self) -> None:
        self.assertEqual(
            _reattach_dialogue_speaker_label("A: سؤال بلا إجابة بعد.", "B: هذا هو الجواب."),
            "B: هذا هو الجواب.",
        )

    def test_reattach_dialogue_speaker_label_is_noop_for_plain_monologue(self) -> None:
        self.assertEqual(
            _reattach_dialogue_speaker_label("خطاف عادي بلا تسمية متحدث.", "بقية الجملة العادية."),
            "بقية الجملة العادية.",
        )

    def test_voice_failure_journal_records_gemini_only_no_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "journal.json"
            journal = _Journal(path, runner_sha="a" * 40, engine_sha="b" * 40)
            for stage in STAGES:
                if stage == "voice":
                    break
                journal.reuse(stage)

            def fail():
                raise VoiceInfrastructureError(
                    charon_attempts=3,
                    charon_reason="gemini_3_8_http_429",
                    retry_after_seconds=125.5,
                )

            with self.assertRaises(VoiceInfrastructureError):
                journal.run("voice", fail)

            voice_failure = journal.payload["voice_failure"]
            self.assertEqual(voice_failure["provider"], GEMINI38_PROVIDER)
            self.assertEqual(voice_failure["charon_attempts"], 3)
            self.assertEqual(voice_failure["tts_wire_attempts"], 3)
            self.assertEqual(voice_failure["tts_cache_hits"], 0)
            self.assertEqual(voice_failure["retry_after_seconds"], 125.5)
            self.assertIn("retry_at_utc", voice_failure)
            self.assertFalse(voice_failure["fallback_used"])

    def test_retired_fallback_workflows_are_absent(self) -> None:
        root = Path(".github/workflows")
        self.assertFalse((root / "piper-isolation.yml").exists())
        self.assertFalse((root / "voice-fallback-acceptance.yml").exists())


if __name__ == "__main__":
    unittest.main()

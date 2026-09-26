from __future__ import annotations

import json
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

from clean_v2.media import (
    GeminiPrimaryNabraFallbackSynthesizer,
    VoiceInfrastructureError,
    _remove_pinned_engine_tail_silence,
)
from clean_v2.audio_mastering import (
    CHARON_CORRECTIVE_FILTER,
    NABRA_CORRECTIVE_FILTER,
    NABRA_MASTERING_PROFILE,
)
from clean_v2.identity_sequence import PRAYER_SENTENCE, SHORT_CHANNEL_DEFINITION
from clean_v2.nabra_voice import (
    NabraVoiceSynthesizer,
    NABRA_REFERENCE_PROFILE,
    NABRA_SPEED,
    NABRA_VOICE,
    _diacritize_preserving_explicit_marks,
    _g2p_preserving_breath_punctuation,
)
from clean_v2.pipeline import _synthesize_sectioned_voice


class _FakeNabra:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    def synthesize(self, transcript: str, output_path: Path) -> Path:
        self.calls += 1
        if self.fail:
            raise RuntimeError("nabra_fixture_failure")
        output_path.write_bytes(b"N" * 2048)
        return output_path


class _FakeContinuousNabraRoute:
    def __init__(self) -> None:
        self.calls = 0
        self.nabra_continuous_ready = True
        self.last_provider = "nabra:af_msa"
        self.fallback_used = True
        self.charon_attempts = 3
        self.voice_approval_status = "human_approved_fallback"
        self.voice_reference_profile = NABRA_REFERENCE_PROFILE

    def synthesize_nabra_continuous(self, parts, output_path: Path):
        self.calls += 1
        sample_rate = 24000
        cursor = 0.0
        marks = []
        for item in parts:
            start = cursor
            speech_end = start + 0.20
            pause_end = speech_end + 0.06
            marks.append(
                {
                    "role": item["role"],
                    "text": item["text"],
                    "start_seconds": start,
                    "speech_end_seconds": speech_end,
                    "pause_end_seconds": pause_end,
                }
            )
            cursor = pause_end
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(output_path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(b"\x00\x00" * int(round(cursor * sample_rate)))
        return {
            "parts": marks,
            "single_continuous_inference": True,
            "external_silence_insertions": 0,
        }


class NabraRouteTests(unittest.TestCase):
    def test_approved_nabra_profile_is_locked(self) -> None:
        self.assertEqual(NABRA_VOICE, "af_msa")
        self.assertEqual(NABRA_SPEED, 0.87)
        self.assertEqual(
            NABRA_REFERENCE_PROFILE,
            "nabra-82m-v0.1:af_msa:0.87:native-pauses-v1",
        )

    def test_long_nabra_phonemes_split_only_for_model_limit(self) -> None:
        source = (
            ("a" * 210)
            + ", "
            + ("b" * 210)
            + "… "
            + ("c" * 210)
        )
        fragments = NabraVoiceSynthesizer._split_phonemes_for_model_limit(source)

        self.assertGreater(len(fragments), 1)
        for index, (spoken, marker) in enumerate(fragments):
            rendered = (spoken + (" " + marker if marker else "")).strip()
            self.assertLessEqual(len(rendered), 500)
            self.assertTrue(spoken)
            if index < len(fragments) - 1:
                self.assertIsNotNone(marker)
        self.assertIsNone(fragments[-1][1])

    def test_short_nabra_phonemes_stay_one_fragment(self) -> None:
        fragments = NabraVoiceSynthesizer._split_phonemes_for_model_limit(
            "abc def ghi"
        )
        self.assertEqual(fragments, [("abc def ghi", None)])

    def test_camel_diacritizer_preserves_writer_marked_ambiguous_word(self) -> None:
        class FakeFrontend:
            def diacritize(self, text: str) -> str:
                self.input = text
                return "أَنَا أَقُولُ عَلَمًا اليَوْمَ"

        frontend = FakeFrontend()
        prepared = _diacritize_preserving_explicit_marks(
            frontend,
            "انا اقول عَلَم اليوم",
        )

        self.assertEqual(frontend.input, "انا اقول عَلَم اليوم")
        self.assertEqual(prepared, "أَنَا أَقُولُ عَلَم اليَوْمَ")

    def test_camel_diacritizer_uses_context_for_unmarked_text(self) -> None:
        class FakeFrontend:
            def diacritize(self, text: str) -> str:
                return "هَذَا نَصٌّ وَاضِحٌ"

        prepared = _diacritize_preserving_explicit_marks(
            FakeFrontend(),
            "هذا نص واضح",
        )
        self.assertEqual(prepared, "هَذَا نَصٌّ وَاضِحٌ")

    def test_g2p_preserves_writer_breath_marks_and_intentional_tashkeel(self) -> None:
        class FakePipeline:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def g2p(self, text: str):
                self.calls.append(text)
                return f"<{text}>", None

        pipeline = FakePipeline()
        phonemes = _g2p_preserving_breath_punctuation(
            pipeline,
            "لا تُحمِّل العبارة، ثم قُلها بوضوح؛ ولا تُسرِع.",
        )

        self.assertEqual(
            pipeline.calls,
            [
                "لا تُحمِّل العبارة",
                "ثم قُلها بوضوح",
                "ولا تُسرِع.",
            ],
        )
        self.assertEqual(phonemes.count(","), 2)
        self.assertIn("تُحمِّل", phonemes)
        self.assertIn("قُلها", phonemes)
        self.assertIn("تُسرِع", phonemes)

    def test_approved_voices_use_neutral_mastering_without_covering_timbre(self) -> None:
        self.assertEqual(NABRA_MASTERING_PROFILE, "nabra-loudness-only-v1")
        self.assertEqual(NABRA_CORRECTIVE_FILTER, "")
        self.assertEqual(CHARON_CORRECTIVE_FILTER, "")

    def test_nabra_locked_route_uses_one_continuous_pass_and_native_pause_units(self) -> None:
        sections = [
            {
                "id": "s1",
                "narration": (
                    f"هذا هو الهوك. {PRAYER_SENTENCE} "
                    f"{SHORT_CHANNEL_DEFINITION} وهذه بداية الموضوع."
                ),
            },
            {
                "id": "s2",
                "narration": "الفكرة تتقدم هنا بجملة واضحة.",
            },
            {
                "id": "s3",
                "narration": "وهنا نصل إلى الخلاصة. ابدأ بخطوة واحدة.",
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            route = _FakeContinuousNabraRoute()
            result = _synthesize_sectioned_voice(
                route,
                sections,
                root / "narration.wav",
                fmt="short",
                identity_definition=SHORT_CHANNEL_DEFINITION,
            )
            report = json.loads(
                (root / "voice-sections.json").read_text(encoding="utf-8")
            )

        self.assertEqual(route.calls, 1)
        self.assertEqual(result["voice_provider"], "nabra:af_msa")
        self.assertTrue(result["single_continuous_inference"])
        self.assertEqual(result["external_silence_insertions"], 0)
        chunks = [
            chunk
            for section in report["sections"]
            for chunk in section["chunks"]
        ]
        intro = [item for item in chunks if item["role"] == "intro_silence"]
        final = [item for item in chunks if item["role"] == "final_silence"]
        self.assertEqual(len(intro), 1)
        self.assertEqual(len(final), 1)
        self.assertEqual(intro[0]["provider"], "nabra_native_pause")
        self.assertEqual(final[0]["provider"], "nabra_native_pause")
        self.assertNotIn(
            "deterministic_silence",
            {str(item.get("provider") or "") for item in chunks},
        )

    def _patch_identity(self):
        return mock.patch.multiple(
            "clean_v2.media",
            _legacy_voice_identity=mock.DEFAULT,
            _assert_human_approved_voice_reference=mock.DEFAULT,
        )

    def test_production_entrypoint_is_charon_then_nabra_only(self) -> None:
        source = Path("clean_v2/__main__.py").read_text(encoding="utf-8")
        self.assertIn("GeminiPrimaryNabraFallbackSynthesizer", source)
        self.assertNotIn("GeminiPrimaryPiperFallbackSynthesizer", source)
        self.assertNotIn("AzureF0NeuralVoiceSynthesizer", source)

    def test_charon_tail_cleanup_removes_only_exact_engine_zero_tail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "charon.wav"
            sample_rate = 24000
            spoken_frames = 2400
            tail_frames = 1200
            with wave.open(str(target), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(sample_rate)
                wav.writeframes(b"\x01\x00" * spoken_frames)
                wav.writeframes(b"\x00\x00" * tail_frames)

            changed = _remove_pinned_engine_tail_silence(
                target,
                "نص تجريبي.",
                expected_seconds=tail_frames / sample_rate,
            )

            self.assertTrue(changed)
            with wave.open(str(target), "rb") as wav:
                self.assertEqual(wav.getnframes(), spoken_frames)
                payload = wav.readframes(spoken_frames)
            self.assertEqual(payload, b"\x01\x00" * spoken_frames)

    def test_charon_tail_cleanup_never_cuts_nonzero_audio(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "charon.wav"
            sample_rate = 24000
            total_frames = 3600
            payload = b"\x01\x00" * total_frames
            with wave.open(str(target), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(sample_rate)
                wav.writeframes(payload)

            before = target.read_bytes()
            changed = _remove_pinned_engine_tail_silence(
                target,
                "نص تجريبي.",
                expected_seconds=0.05,
            )

            self.assertFalse(changed)
            self.assertEqual(target.read_bytes(), before)

    def test_short_charon_style_does_not_reopen_every_chunk(self) -> None:
        from clean_v2.media import SHORT_CHARON_STYLE

        lowered = SHORT_CHARON_STYLE.casefold()
        self.assertIn("same calm conversational cadence", lowered)
        self.assertIn("do not reset", lowered)
        self.assertNotIn("firmer in intent", lowered)
        self.assertNotIn("clean first-word attack", lowered)

    def test_charon_natural_style_is_used_for_regular_and_short_calls(self) -> None:
        from clean_v2.media import CHARON_NATURAL_STYLE

        captured: list[str] = []

        def gemini(*args, **kwargs):
            captured.append(str(kwargs.get("style") or ""))
            Path(args[2]).write_bytes(b"C" * 2048)

        for primary_only in (False, True):
            backup = _FakeNabra()
            with tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "voice.wav"
                synth = GeminiPrimaryNabraFallbackSynthesizer("key", nabra=backup)
                with mock.patch(
                    "clean_v2.media._legacy_voice_identity",
                    return_value=("Charon", "Orus"),
                ), mock.patch(
                    "clean_v2.media._assert_human_approved_voice_reference",
                    return_value="approved-charon",
                ), mock.patch(
                    "clean_v2.media._legacy_gemini_synthesize",
                    side_effect=gemini,
                ):
                    synth.synthesize(
                        "نص عربي طبيعي متصل.",
                        target,
                        primary_only=primary_only,
                    )
                self.assertEqual(backup.calls, 0)

        self.assertEqual(captured, [CHARON_NATURAL_STYLE, CHARON_NATURAL_STYLE])

    def test_charon_success_never_calls_nabra(self) -> None:
        backup = _FakeNabra()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "voice.wav"
            synth = GeminiPrimaryNabraFallbackSynthesizer("key", nabra=backup)
            with mock.patch(
                "clean_v2.media._legacy_voice_identity",
                return_value=("Charon", "Orus"),
            ), mock.patch(
                "clean_v2.media._assert_human_approved_voice_reference",
                return_value="approved-charon",
            ), mock.patch(
                "clean_v2.media._legacy_gemini_synthesize",
                side_effect=lambda *_a, **_k: target.write_bytes(b"C" * 2048),
            ):
                synth.synthesize("نص عربي واضح.", target)

            self.assertEqual(synth.last_provider, "gemini:Charon")
            self.assertFalse(synth.fallback_used)
            self.assertEqual(backup.calls, 0)

    def test_first_chunk_charon_failure_locks_nabra_for_following_chunks(self) -> None:
        backup = _FakeNabra()
        gemini_calls = 0

        def fail_gemini(*_args, **_kwargs):
            nonlocal gemini_calls
            gemini_calls += 1
            raise RuntimeError("http 429")

        with tempfile.TemporaryDirectory() as tmp:
            synth = GeminiPrimaryNabraFallbackSynthesizer("key", nabra=backup)
            with mock.patch(
                "clean_v2.media._legacy_voice_identity",
                return_value=("Charon", "Orus"),
            ), mock.patch(
                "clean_v2.media._legacy_gemini_synthesize",
                side_effect=fail_gemini,
            ), mock.patch(
                "clean_v2.media._charon_retry_delay",
                return_value=0.0,
            ):
                synth.synthesize("الجملة الأولى.", Path(tmp) / "one.wav")
                first_gemini_calls = gemini_calls
                synth.synthesize("الجملة الثانية.", Path(tmp) / "two.wav")

            self.assertEqual(synth.last_provider, "nabra:af_msa")
            self.assertTrue(synth.fallback_used)
            self.assertEqual(backup.calls, 2)
            self.assertEqual(gemini_calls, first_gemini_calls)

    def test_short_style_flag_still_allows_nabra_before_route_lock(self) -> None:
        backup = _FakeNabra()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "voice.wav"
            synth = GeminiPrimaryNabraFallbackSynthesizer("", nabra=backup)
            with mock.patch(
                "clean_v2.media._legacy_voice_identity",
                return_value=("Charon", "Orus"),
            ):
                synth.synthesize("نص شورت واضح.", target, primary_only=True)

            self.assertEqual(synth.last_provider, "nabra:af_msa")
            self.assertTrue(synth.fallback_used)
            self.assertEqual(backup.calls, 1)

    def test_both_routes_fail_closed(self) -> None:
        backup = _FakeNabra(fail=True)
        with tempfile.TemporaryDirectory() as tmp:
            synth = GeminiPrimaryNabraFallbackSynthesizer("", nabra=backup)
            with mock.patch(
                "clean_v2.media._legacy_voice_identity",
                return_value=("Charon", "Orus"),
            ):
                with self.assertRaises(VoiceInfrastructureError):
                    synth.synthesize("اختبار.", Path(tmp) / "voice.wav")
            self.assertEqual(backup.calls, 1)


if __name__ == "__main__":
    unittest.main()

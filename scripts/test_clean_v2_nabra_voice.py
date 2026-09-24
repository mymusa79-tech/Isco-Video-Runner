from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clean_v2.media import (
    GeminiPrimaryNabraFallbackSynthesizer,
    VoiceInfrastructureError,
)


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


class NabraRouteTests(unittest.TestCase):
    def _patch_identity(self):
        return mock.patch.multiple(
            "clean_v2.media",
            _legacy_voice_identity=mock.DEFAULT,
            _assert_human_approved_voice_reference=mock.DEFAULT,
        )

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

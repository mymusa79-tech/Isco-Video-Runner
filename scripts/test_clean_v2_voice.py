from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from clean_v2.media import GeminiPrimaryPiperFallbackSynthesizer


class CleanV2VoiceRoutingTests(unittest.TestCase):
    def _synthesizer(self, root: Path) -> GeminiPrimaryPiperFallbackSynthesizer:
        return GeminiPrimaryPiperFallbackSynthesizer(
            "gemini-test-key",
            root / "ar_JO-kareem-medium.onnx",
            None,
            tts_model="gemini-3.1-flash-tts-preview",
        )

    def test_charon_is_attempted_first_and_piper_is_not_used_on_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "narration.wav"
            synth = self._synthesizer(root)
            events: list[str] = []

            def gemini(*args, **kwargs):
                events.append("gemini")
                self.assertEqual(kwargs["voice"], "Charon")
                self.assertEqual(kwargs["model"], "gemini-3.1-flash-tts-preview")
                Path(args[2]).write_bytes(b"G" * 2048)
                return Path(args[2])

            def piper(*_args, **_kwargs):
                events.append("piper")
                raise AssertionError("Piper must not run after successful Gemini TTS")

            with patch("clean_v2.media._legacy_voice_identity", return_value=("Charon", "Orus")), \
                 patch("clean_v2.media._legacy_gemini_synthesize", side_effect=gemini), \
                 patch.object(synth.piper, "synthesize", side_effect=piper):
                result = synth.synthesize("هذا اختبار للصوت الأساسي.", output)

            self.assertEqual(result, output)
            self.assertEqual(events, ["gemini"])
            self.assertEqual(synth.last_provider, "gemini:Charon")
            self.assertFalse(synth.fallback_used)

    def test_piper_runs_only_after_gemini_tts_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "narration.wav"
            synth = self._synthesizer(root)
            events: list[str] = []

            def gemini(*_args, **_kwargs):
                events.append("gemini")
                raise RuntimeError("synthetic Gemini TTS failure")

            def piper(_transcript: str, target: Path) -> Path:
                events.append("piper")
                target.write_bytes(b"P" * 2048)
                return target

            with patch("clean_v2.media._legacy_voice_identity", return_value=("Charon", "Orus")), \
                 patch("clean_v2.media._legacy_gemini_synthesize", side_effect=gemini), \
                 patch.object(synth.piper, "synthesize", side_effect=piper):
                result = synth.synthesize("هذا اختبار لمسار الاحتياط.", output)

            self.assertEqual(result, output)
            self.assertEqual(events, ["gemini", "piper"])
            self.assertEqual(synth.last_provider, "piper-local:ar_JO-kareem-medium")
            self.assertTrue(synth.fallback_used)

    def test_voice_identity_mismatch_fails_closed_before_any_tts_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            synth = self._synthesizer(root)
            with patch("clean_v2.media._legacy_voice_identity", return_value=("Gacrux", "Orus")), \
                 patch("clean_v2.media._legacy_gemini_synthesize") as gemini, \
                 patch.object(synth.piper, "synthesize") as piper:
                with self.assertRaisesRegex(RuntimeError, "primary voice identity mismatch"):
                    synth.synthesize("اختبار هوية الصوت.", root / "narration.wav")
            gemini.assert_not_called()
            piper.assert_not_called()


if __name__ == "__main__":
    unittest.main()

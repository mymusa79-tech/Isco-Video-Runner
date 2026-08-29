from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import scripts.tts_durable_cache as durable
from scripts import voice_mesh


class TtsDurableCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        voice_mesh._voice_provenance.clear()

    def _env(self, root: Path) -> dict[str, str]:
        return {
            "ISCO_TTS_CACHE_PATH": str(root / "cache"),
            "ISCO_APPROVED_BRIEF_SHA256": "a" * 64,
            "ISCO_ENGINE_SHA": "b" * 40,
            "ISCO_DIALOGUE_QA": "0",
        }

    @staticmethod
    def _call(boundary, output: Path, transcript: str = "نص عربي للاختبار") -> Path:
        return boundary(
            None,
            object(),
            object(),
            task_id="TTS_SECTION_S1",
            api_key="secret-never-persisted",
            transcript=transcript,
            output=output,
            model="gemini-3.1-flash-tts-preview",
            voice="Charon",
            style="warm",
        )

    def test_second_identical_section_is_restored_without_provider_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls = {"count": 0}

            def original(*args, **kwargs):
                calls["count"] += 1
                output = Path(kwargs["output"])
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(b"A" * 4096)
                voice_mesh.record_voice_provenance(output, provider="gemini", fallback_used=False)
                return output

            with patch.dict(os.environ, self._env(root), clear=False), \
                    patch.object(durable.orchestrator, "_synthesize_tts_section", original), \
                    patch.object(voice_mesh, "qa_voice_output") as qa:
                durable.install_tts_durable_cache()
                boundary = durable.orchestrator._synthesize_tts_section
                first = self._call(boundary, root / "run1" / "audio" / "01.wav")
                second = self._call(boundary, root / "run2" / "audio" / "01.wav")

            self.assertEqual(calls["count"], 1)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            qa.assert_called_once_with(second, "نص عربي للاختبار")
            provenance = voice_mesh.consume_voice_provenance(second)
            self.assertEqual(provenance["provider"], "durable-cache:gemini")
            self.assertFalse(provenance["fallback_used"])
            self.assertTrue(durable.prepare_cache_for_persistence(root / "cache"))

    def test_transcript_change_is_a_semantic_cache_miss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls = {"count": 0}

            def original(*args, **kwargs):
                calls["count"] += 1
                output = Path(kwargs["output"])
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(bytes([64 + calls["count"]]) * 4096)
                voice_mesh.record_voice_provenance(output, provider="gemini", fallback_used=False)
                return output

            with patch.dict(os.environ, self._env(root), clear=False), \
                    patch.object(durable.orchestrator, "_synthesize_tts_section", original), \
                    patch.object(voice_mesh, "qa_voice_output"):
                durable.install_tts_durable_cache()
                boundary = durable.orchestrator._synthesize_tts_section
                self._call(boundary, root / "run1" / "audio" / "01.wav", "النص الأول")
                self._call(boundary, root / "run2" / "audio" / "01.wav", "النص الثاني")

            self.assertEqual(calls["count"], 2)

    def test_tampered_cached_audio_is_never_reused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls = {"count": 0}

            def original(*args, **kwargs):
                calls["count"] += 1
                output = Path(kwargs["output"])
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(bytes([70 + calls["count"]]) * 4096)
                voice_mesh.record_voice_provenance(output, provider="piper-local", fallback_used=True)
                return output

            with patch.dict(os.environ, self._env(root), clear=False), \
                    patch.object(durable.orchestrator, "_synthesize_tts_section", original), \
                    patch.object(voice_mesh, "qa_voice_output") as qa:
                durable.install_tts_durable_cache()
                boundary = durable.orchestrator._synthesize_tts_section
                self._call(boundary, root / "run1" / "audio" / "01.wav")
                entries = list((root / "cache" / "entries").iterdir())
                self.assertEqual(len(entries), 1)
                cached_audio = entries[0] / durable.AUDIO_FILENAME
                cached_audio.write_bytes(b"X" + cached_audio.read_bytes()[1:])
                regenerated = self._call(boundary, root / "run2" / "audio" / "01.wav")

            self.assertEqual(calls["count"], 2)
            qa.assert_not_called()
            self.assertEqual(regenerated.read_bytes(), bytes([72]) * 4096)

    def test_cache_layer_does_not_hide_original_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def original(*args, **kwargs):
                raise RuntimeError("tts section failed")

            with patch.dict(os.environ, self._env(root), clear=False), \
                    patch.object(durable.orchestrator, "_synthesize_tts_section", original):
                durable.install_tts_durable_cache()
                boundary = durable.orchestrator._synthesize_tts_section
                with self.assertRaisesRegex(RuntimeError, "tts section failed"):
                    self._call(boundary, root / "run1" / "audio" / "01.wav")
            self.assertFalse(durable.prepare_cache_for_persistence(root / "cache"))


if __name__ == "__main__":
    unittest.main()

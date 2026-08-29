from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import isco_video_agent.orchestrator as orchestrator
import scripts.audio_semantic_integrity as audio_semantic
import scripts.tts_durable_section_cache as cache
import scripts.voice_identity_observer as voice_identity
import scripts.voice_mesh as voice_mesh


class TtsDurableSectionCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cache_dir = self.root / "cache"
        self.out_dir = self.root / "output" / "run-1" / "audio"
        self.out_dir.mkdir(parents=True)
        self.calls = 0
        self.original_boundary = orchestrator._synthesize_tts_section
        self.original_observer_boundary = voice_identity._original_synthesize_tts_section
        self.original_audio_state = dict(audio_semantic._tts_by_task)
        self.original_audio_paths = dict(audio_semantic._tts_by_path)

        def provider(
            ledger,
            circuit,
            budget,
            *,
            task_id: str,
            api_key: str,
            transcript: str,
            output: Path,
            model: str,
            voice: str,
            style: str,
        ) -> Path:
            self.calls += 1
            output = Path(output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes((f"audio-{self.calls}-{transcript}".encode("utf-8") + b"x" * 4096))
            voice_mesh._record_voice_provenance(output, provider="gemini", fallback_used=False)
            return output

        orchestrator._synthesize_tts_section = provider
        self.env = patch.dict(
            os.environ,
            {
                "ISCO_TTS_CACHE_DIR": str(self.cache_dir),
                "ISCO_DIALOGUE_QA": "0",
            },
            clear=False,
        )
        self.env.start()
        self.qa = patch.object(voice_mesh, "_qa")
        self.qa_mock = self.qa.start()
        cache.install_tts_durable_section_cache()

    def tearDown(self) -> None:
        orchestrator._synthesize_tts_section = self.original_boundary
        voice_identity._original_synthesize_tts_section = self.original_observer_boundary
        audio_semantic._tts_by_task.clear()
        audio_semantic._tts_by_task.update(self.original_audio_state)
        audio_semantic._tts_by_path.clear()
        audio_semantic._tts_by_path.update(self.original_audio_paths)
        voice_mesh._voice_provenance.clear()
        self.qa.stop()
        self.env.stop()
        self.tmp.cleanup()

    def _call(
        self,
        output: Path,
        *,
        task_id: str = "TTS_SECTION_01",
        transcript: str = "نص ثابت للاختبار",
        model: str = "gemini-3.1-flash-tts-preview",
        voice: str = "Charon",
        style: str = "Emotion: calm.",
    ) -> Path:
        return orchestrator._synthesize_tts_section(
            object(),
            object(),
            object(),
            task_id=task_id,
            api_key="secret-never-fingerprinted",
            transcript=transcript,
            output=output,
            model=model,
            voice=voice,
            style=style,
        )

    def test_identical_section_resumes_without_second_provider_call(self) -> None:
        first = self._call(self.out_dir / "01.wav")
        first_bytes = first.read_bytes()
        second = self._call(self.out_dir / "01-resumed.wav")

        self.assertEqual(self.calls, 1)
        self.assertEqual(second.read_bytes(), first_bytes)
        self.assertEqual(voice_mesh.consume_voice_provenance(second)["provider"], "gemini")
        audit = json.loads((self.out_dir.parent / cache.AUDIT_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(audit["summary"]["hits"], 1)
        self.assertEqual(audit["summary"]["misses"], 1)

    def test_semantic_inputs_invalidate_independently(self) -> None:
        self._call(self.out_dir / "base.wav")
        self._call(self.out_dir / "text.wav", transcript="نص مختلف")
        self._call(self.out_dir / "model.wav", model="different-model")
        self._call(self.out_dir / "voice.wav", voice="DifferentVoice")
        self._call(self.out_dir / "style.wav", style="Emotion: energetic.")
        with patch.dict(os.environ, {"ISCO_DIALOGUE_QA": "1"}, clear=False):
            self._call(
                self.out_dir / "dialogue.wav",
                transcript="السائل: لماذا؟\nالمجيب: لأننا نختبر.",
            )
        self.assertEqual(self.calls, 6)

    def test_corrupt_cached_bytes_are_evicted_and_regenerated(self) -> None:
        kwargs = {
            "transcript": "نص ثابت للاختبار",
            "model": "gemini-3.1-flash-tts-preview",
            "voice": "Charon",
            "style": "Emotion: calm.",
            "dialogue": False,
        }
        fingerprint, _ = cache.semantic_fingerprint(**kwargs)
        self._call(self.out_dir / "first.wav")
        cached = self.cache_dir / fingerprint / cache.CACHE_AUDIO_FILENAME
        cached.write_bytes(b"tampered")

        resumed = self._call(self.out_dir / "second.wav")
        self.assertEqual(self.calls, 2)
        self.assertGreater(resumed.stat().st_size, len(b"tampered"))

    def test_symlinked_cached_audio_is_never_trusted(self) -> None:
        self._call(self.out_dir / "first.wav")
        fingerprint, _ = cache.semantic_fingerprint(
            transcript="نص ثابت للاختبار",
            model="gemini-3.1-flash-tts-preview",
            voice="Charon",
            style="Emotion: calm.",
            dialogue=False,
        )
        entry = self.cache_dir / fingerprint
        cached = entry / cache.CACHE_AUDIO_FILENAME
        target = self.root / "outside.wav"
        target.write_bytes(b"outside-secret")
        cached.unlink()
        try:
            cached.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")

        resumed = self._call(self.out_dir / "second.wav")
        self.assertEqual(self.calls, 2)
        self.assertNotEqual(resumed.read_bytes(), b"outside-secret")

    def test_failed_synthesis_is_never_cached(self) -> None:
        current = cache._original_synthesize_tts_section

        def fail(*_args, **_kwargs):
            raise RuntimeError("provider failed")

        cache._original_synthesize_tts_section = fail
        try:
            with self.assertRaisesRegex(RuntimeError, "provider failed"):
                self._call(self.out_dir / "failed.wav")
        finally:
            cache._original_synthesize_tts_section = current

        fingerprint, _ = cache.semantic_fingerprint(
            transcript="نص ثابت للاختبار",
            model="gemini-3.1-flash-tts-preview",
            voice="Charon",
            style="Emotion: calm.",
            dialogue=False,
        )
        self.assertFalse((self.cache_dir / fingerprint).exists())

    def test_cache_hit_runs_voice_qa_again(self) -> None:
        self._call(self.out_dir / "first.wav")
        first_count = self.qa_mock.call_count
        self._call(self.out_dir / "second.wav")
        self.assertGreater(self.qa_mock.call_count, first_count)
        self.assertEqual(self.calls, 1)

    def test_audio_semantic_integrity_still_binds_a_cache_hit(self) -> None:
        self._call(self.out_dir / "first.wav")
        provider_calls = self.calls
        audio_semantic.reset_audio_semantic_integrity_state_for_tests()
        semantic_boundary = audio_semantic._wrap_tts(orchestrator._synthesize_tts_section)
        resumed = self.out_dir / "semantic-resume.wav"
        semantic_boundary(
            object(), object(), object(),
            task_id="TTS_SECTION_01",
            api_key="secret",
            transcript="نص ثابت للاختبار",
            output=resumed,
            model="gemini-3.1-flash-tts-preview",
            voice="Charon",
            style="Emotion: calm.",
        )
        self.assertEqual(self.calls, provider_calls)
        binding = audio_semantic._tts_by_task["TTS_SECTION_01"]
        self.assertEqual(binding.audio_sha256, cache._sha256_file(resumed))

    def test_voice_identity_observer_still_runs_on_cache_hit(self) -> None:
        self._call(self.out_dir / "first.wav")
        provider_calls = self.calls
        with patch.object(voice_identity, "observe_output") as observe:
            voice_identity.install_voice_identity_observer()
            resumed = self._call(self.out_dir / "observer-resume.wav")
        self.assertEqual(self.calls, provider_calls)
        observe.assert_called_once()
        self.assertEqual(observe.call_args.kwargs["output"], resumed)

    def test_prepare_for_persistence_removes_invalid_entries(self) -> None:
        self._call(self.out_dir / "first.wav")
        bad = self.cache_dir / ("a" * 64)
        bad.mkdir()
        (bad / cache.CACHE_MANIFEST_FILENAME).write_text("{}", encoding="utf-8")
        self.assertTrue(cache.prepare_cache_for_persistence(self.cache_dir))
        self.assertFalse(bad.exists())


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import scripts.voice_identity_observer as observer
import scripts.voice_mesh as voice_mesh


_PROFILE = {
    "profile_version": "channel-voice-roster-v1",
    "embedding": {
        "backend": "speechbrain-ecapa-voxceleb",
        "model_source": "speechbrain/spkrec-ecapa-voxceleb",
        "model_revision": "test-revision",
        "sample_rate_hz": 16000,
        "window_seconds": 4.0,
    },
    "profiles": {
        "primary": {"voice_name": "Charon", "centroid": [1.0, 0.0]},
        "questioner": {"voice_name": "Orus", "centroid": [0.0, 1.0]},
    },
}


class VoiceRosterContractTests(unittest.TestCase):
    def test_fixed_roster_is_charon_orus(self) -> None:
        self.assertEqual(voice_mesh.DIALOGUE_RESPONDER_VOICE, "Charon")
        self.assertEqual(voice_mesh.DIALOGUE_QUESTIONER_VOICE, "Orus")

    def test_voice_provenance_is_consumed_once(self) -> None:
        output = Path("voice.wav")
        voice_mesh._record_voice_provenance(output, provider="piper-local", fallback_used=True)
        self.assertEqual(
            voice_mesh.consume_voice_provenance(output),
            {"provider": "piper-local", "fallback_used": True},
        )
        self.assertEqual(
            voice_mesh.consume_voice_provenance(output),
            {"provider": "unknown", "fallback_used": None},
        )


class VoiceObserverArtifactTests(unittest.TestCase):
    def _analysis(self) -> dict:
        return {
            "speaker_similarity": 0.91,
            "window_scores": [
                {"window": "start", "start_seconds": 0.0, "duration_seconds": 4.0, "primary_similarity": 0.9},
                {"window": "middle", "start_seconds": 4.0, "duration_seconds": 4.0, "primary_similarity": 0.92},
                {"window": "end", "start_seconds": 8.0, "duration_seconds": 4.0, "primary_similarity": 0.91},
            ],
            "internal_consistency": {"median": 0.88, "minimum": 0.84},
            "dialogue_role_segmentation": None,
            "f0_median": 118.0,
            "f0_p10": 92.0,
            "f0_p90": 176.0,
        }

    def test_non_dialogue_is_measured_but_never_pass_or_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "audio" / "01.wav"
            output.parent.mkdir(parents=True)
            with patch.object(observer, "_load_profile", return_value=_PROFILE), \
                    patch.object(observer, "_actual_provider", return_value={"provider": "gemini", "fallback_used": False}), \
                    patch.object(observer, "_analyze_wav", return_value=self._analysis()):
                observer.observe_output(
                    task_id="TTS_SECTION_01",
                    transcript="هذا نص واحد.",
                    output=output,
                    model="gemini-3.1-flash-tts-preview",
                    requested_voice="Charon",
                )
            data = json.loads((root / observer.AUDIT_FILENAME).read_text(encoding="utf-8"))
            self.assertEqual(data["mode"], "observe_only")
            self.assertFalse(data["calibrated"])
            self.assertEqual(data["enforcement"], "disabled")
            self.assertEqual(data["roster"]["primary"], "Charon")
            self.assertEqual(data["roster"]["questioner"], "Orus")
            entry = data["sections"][0]
            self.assertEqual(entry["decision"], "uncalibrated")
            self.assertEqual(entry["reference_profiles"], ["primary"])
            self.assertEqual(entry["provider"], "gemini")
            self.assertFalse(entry["fallback_used"])
            self.assertNotIn(entry["decision"], {"pass", "block", "retry"})

    def test_dialogue_reports_both_references_without_claiming_time_aligned_roles(self) -> None:
        analysis = self._analysis()
        analysis["speaker_similarity"] = None
        analysis["dialogue_role_segmentation"] = "not_time_aligned_v1"
        analysis["window_scores"][0]["questioner_similarity"] = 0.72
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "audio" / "02.wav"
            output.parent.mkdir(parents=True)
            with patch.object(observer, "_load_profile", return_value=_PROFILE), \
                    patch.object(observer, "_actual_provider", return_value={"provider": "gemini-multispeaker", "fallback_used": False}), \
                    patch.object(observer, "_analyze_wav", return_value=analysis):
                observer.observe_output(
                    task_id="TTS_SECTION_02",
                    transcript="A: لماذا؟\nB: لأننا نختبر.",
                    output=output,
                    model="gemini-3.1-flash-tts-preview",
                    requested_voice="Charon",
                )
            entry = json.loads((root / observer.AUDIT_FILENAME).read_text(encoding="utf-8"))["sections"][0]
            self.assertTrue(entry["dialogue_mode"])
            self.assertEqual(entry["reference_profiles"], ["primary", "questioner"])
            self.assertEqual(entry["dialogue_role_segmentation"], "not_time_aligned_v1")
            self.assertEqual(entry["decision"], "uncalibrated")

    def test_analysis_error_is_recorded_and_remains_fail_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "audio" / "03.wav"
            output.parent.mkdir(parents=True)
            with patch.object(observer, "_load_profile", return_value=_PROFILE), \
                    patch.object(observer, "_actual_provider", return_value={"provider": "piper-local", "fallback_used": True}), \
                    patch.object(observer, "_analyze_wav", side_effect=RuntimeError("broken audio")):
                observer.observe_output(
                    task_id="TTS_SECTION_03",
                    transcript="نص",
                    output=output,
                    model="gemini-3.1-flash-tts-preview",
                    requested_voice="Charon",
                )
            data = json.loads((root / observer.AUDIT_FILENAME).read_text(encoding="utf-8"))
            entry = data["sections"][0]
            self.assertEqual(entry["decision"], "audit_error")
            self.assertEqual(entry["provider"], "piper-local")
            self.assertTrue(entry["fallback_used"])
            self.assertEqual(data["summary"]["audit_errors"], 1)


class VoiceObserverWrapperTests(unittest.TestCase):
    def test_wrapper_calls_original_once_and_never_retries_for_audit(self) -> None:
        original_boundary = observer.orchestrator._synthesize_tts_section
        original_saved = observer._original_synthesize_tts_section
        output = Path("output/test/audio/01.wav")
        production = Mock(return_value=output)
        try:
            observer.orchestrator._synthesize_tts_section = production
            observer._original_synthesize_tts_section = None
            observer.install_voice_identity_observer()
            wrapped = observer.orchestrator._synthesize_tts_section
            with patch.object(observer, "observe_output", side_effect=RuntimeError("observer failed")) as audit:
                result = wrapped(
                    None,
                    object(),
                    object(),
                    task_id="TTS_SECTION_01",
                    api_key="secret",
                    transcript="نص",
                    output=output,
                    model="tts-model",
                    voice="Charon",
                    style="calm",
                )
            self.assertEqual(result, output)
            production.assert_called_once()
            audit.assert_called_once()
        finally:
            observer.orchestrator._synthesize_tts_section = original_boundary
            observer._original_synthesize_tts_section = original_saved

    def test_install_is_idempotent(self) -> None:
        original_boundary = observer.orchestrator._synthesize_tts_section
        original_saved = observer._original_synthesize_tts_section
        production = Mock()
        try:
            observer.orchestrator._synthesize_tts_section = production
            observer._original_synthesize_tts_section = None
            observer.install_voice_identity_observer()
            first = observer.orchestrator._synthesize_tts_section
            observer.install_voice_identity_observer()
            self.assertIs(observer.orchestrator._synthesize_tts_section, first)
        finally:
            observer.orchestrator._synthesize_tts_section = original_boundary
            observer._original_synthesize_tts_section = original_saved


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

import scripts.voice_mesh as voice_mesh


class VoiceMeshGeminiOnlyTests(unittest.TestCase):
    def test_single_voice_uses_gemini38_charon_and_no_fallback(self) -> None:
        output = Path("cloud.wav")
        captured: dict[str, object] = {}

        def fake_gemini(api_key, transcript, target, **kwargs):
            captured.update({"api_key": api_key, "transcript": transcript, **kwargs})
            return target

        with patch.dict(os.environ, {"ISCO_DIALOGUE_QA": "0"}, clear=False), \
                patch.object(voice_mesh, "_gemini38_synthesize", side_effect=fake_gemini), \
                patch.object(voice_mesh, "section_tail_seconds", return_value=0.65), \
                patch.object(voice_mesh, "add_tail_silence_in_place", return_value=output), \
                patch.object(voice_mesh, "_qa") as qa:
            result = voice_mesh.synthesize(
                "key",
                "نص",
                output,
                model="gemini-3.8-flash-tts",
                voice="legacy-ignored",
                style="legacy-ignored",
                attempts=1,
            )

        self.assertEqual(result, output)
        self.assertEqual(captured["model"], "gemini-3.8-flash-tts")
        self.assertEqual(captured["primary_voice"], "Charon")
        self.assertEqual(captured["questioner_voice"], "Orus")
        self.assertEqual(voice_mesh.peek_voice_provenance(output)["provider"], "gemini-3.8:Charon")
        self.assertFalse(voice_mesh.peek_voice_provenance(output)["fallback_used"])
        qa.assert_called_once_with(output, "نص")

    def test_dialogue_maps_legacy_labels_to_orus_charon_metadata(self) -> None:
        transcript = "السائل: لماذا؟\nالمجيب: لأننا نختبر."
        output = Path("dialogue.wav")
        captured: dict[str, object] = {}

        def fake_gemini(api_key, spoken, target, **kwargs):
            captured.update({"api_key": api_key, "spoken": spoken, **kwargs})
            return target

        with patch.dict(os.environ, {"ISCO_DIALOGUE_QA": "1"}, clear=False), \
                patch.object(voice_mesh, "_gemini38_synthesize", side_effect=fake_gemini), \
                patch.object(voice_mesh, "section_tail_seconds", return_value=0.65) as tail_policy, \
                patch.object(voice_mesh, "add_tail_silence_in_place", return_value=output) as add_tail, \
                patch.object(voice_mesh, "_qa"):
            result = voice_mesh.synthesize(
                "key",
                transcript,
                output,
                model="gemini-3.8-flash-tts",
                voice="ignored",
                attempts=1,
            )

        self.assertEqual(result, output)
        self.assertEqual(captured["spoken"], "A: لماذا؟\nB: لأننا نختبر.")
        self.assertEqual(captured["primary_voice"], "Charon")
        self.assertEqual(captured["questioner_voice"], "Orus")
        tail_policy.assert_called_once_with(transcript)
        add_tail.assert_called_once_with(output, 0.65)
        self.assertEqual(
            voice_mesh.peek_voice_provenance(output)["provider"],
            "gemini-3.8:Charon+Orus",
        )
        self.assertFalse(voice_mesh.peek_voice_provenance(output)["fallback_used"])

    def test_cloud_failure_is_not_substituted(self) -> None:
        output = Path("cloud.wav")
        with patch.dict(os.environ, {"ISCO_DIALOGUE_QA": "0"}, clear=False), \
                patch.object(
                    voice_mesh,
                    "_gemini38_synthesize",
                    side_effect=RuntimeError("429 quota exceeded"),
                ), patch.object(voice_mesh, "_qa"):
            with self.assertRaisesRegex(RuntimeError, "429 quota exceeded"):
                voice_mesh.synthesize(
                    "key",
                    "نص",
                    output,
                    model="gemini-3.8-flash-tts",
                    voice="Charon",
                    attempts=1,
                )
        self.assertEqual(
            voice_mesh.peek_voice_provenance(output),
            {"provider": "unknown", "fallback_used": None},
        )

    def test_model_drift_is_rejected_before_provider_call(self) -> None:
        with patch.object(voice_mesh, "_gemini38_synthesize") as provider:
            with self.assertRaisesRegex(RuntimeError, "voice_mesh_model_drift"):
                voice_mesh.synthesize(
                    "key",
                    "نص",
                    Path("cloud.wav"),
                    model="gemini-3.1-flash-tts-preview",
                    voice="Charon",
                    attempts=1,
                )
        provider.assert_not_called()

    def test_local_fallback_is_retired_and_fails_closed(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "voice_local_fallback_retired"):
            voice_mesh.synthesize_local_wav("نص", Path("local.wav"))

    def test_retry_count_is_not_caller_expandable(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "voice_mesh_retry_owner_violation"):
            voice_mesh.synthesize(
                "key",
                "نص",
                Path("cloud.wav"),
                model="gemini-3.8-flash-tts",
                voice="Charon",
                attempts=2,
            )

    def test_install_voice_mesh_patches_cloud_and_fail_closed_local_boundary(self) -> None:
        original_cloud = getattr(voice_mesh.orchestrator, "synthesize_wav", None)
        original_local = getattr(voice_mesh.orchestrator, "synthesize_local_wav", None)
        try:
            with patch(
                "scripts.provider_retry_ownership.certify_provider_retry_ownership",
                return_value={"status": "pass", "provider_calls_executed": 0},
            ) as certify:
                voice_mesh.install_voice_mesh()
            certify.assert_called_once_with()
            self.assertIs(voice_mesh.orchestrator.synthesize_wav, voice_mesh.synthesize)
            self.assertIs(
                voice_mesh.orchestrator.synthesize_local_wav,
                voice_mesh.synthesize_local_wav,
            )
        finally:
            if original_cloud is None:
                delattr(voice_mesh.orchestrator, "synthesize_wav")
            else:
                voice_mesh.orchestrator.synthesize_wav = original_cloud
            if original_local is None:
                delattr(voice_mesh.orchestrator, "synthesize_local_wav")
            else:
                voice_mesh.orchestrator.synthesize_local_wav = original_local


if __name__ == "__main__":
    unittest.main()

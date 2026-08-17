from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

import scripts.voice_mesh as voice_mesh


class VoiceMeshCloudTests(unittest.TestCase):
    def test_module_has_no_gemini_circuit_global(self) -> None:
        self.assertFalse(hasattr(voice_mesh, "_gemini_open"))

    def test_synthesize_forwards_attempts_to_gemini(self) -> None:
        output = Path("cloud.wav")
        with patch.dict(os.environ, {"ISCO_DIALOGUE_QA": "0"}, clear=False), \
                patch.object(voice_mesh, "gemini_synthesize", return_value=output) as gemini, \
                patch.object(voice_mesh, "_qa") as qa:
            result = voice_mesh.synthesize(
                "key", "نص", output,
                model="gemini-tts", voice="Gacrux", style="calm", attempts=1,
            )
        self.assertEqual(result, output)
        gemini.assert_called_once_with(
            "key", "نص", output,
            model="gemini-tts", voice="Gacrux", style="calm", attempts=1,
        )
        qa.assert_called_once_with(output, "نص")

    def test_synthesize_does_not_swallow_cloud_exception(self) -> None:
        output = Path("cloud.wav")
        with patch.dict(os.environ, {"ISCO_DIALOGUE_QA": "0"}, clear=False), \
                patch.object(voice_mesh, "gemini_synthesize", side_effect=RuntimeError("429 quota exceeded")), \
                patch.object(voice_mesh, "_local") as local, \
                patch.object(voice_mesh, "_qa"):
            with self.assertRaisesRegex(RuntimeError, "429 quota exceeded"):
                voice_mesh.synthesize(
                    "key", "نص", output,
                    model="gemini-tts", voice="Gacrux", attempts=1,
                )
        local.assert_not_called()


class VoiceMeshLocalTests(unittest.TestCase):
    def test_local_non_dialogue_uses_piper_and_qa(self) -> None:
        output = Path("local.wav")
        with patch.dict(os.environ, {"ISCO_DIALOGUE_QA": "0"}, clear=False), \
                patch.object(voice_mesh, "_local", return_value=output) as local, \
                patch.object(voice_mesh, "_qa") as qa:
            result = voice_mesh.synthesize_local_wav("نص محلي", output)
        self.assertEqual(result, output)
        local.assert_called_once_with("نص محلي", output)
        qa.assert_called_once_with(output, "نص محلي")

    def test_local_dialogue_preserves_existing_dialogue_fallback(self) -> None:
        transcript = "السائل: لماذا؟\nالمجيب: لأننا نختبر."
        output = Path("dialogue.wav")
        with patch.dict(os.environ, {"ISCO_DIALOGUE_QA": "1"}, clear=False), \
                patch.object(voice_mesh, "_local_dialogue", return_value=output) as local, \
                patch.object(voice_mesh, "_qa") as qa:
            result = voice_mesh.synthesize_local_wav(transcript, output)
        self.assertEqual(result, output)
        local.assert_called_once_with(transcript, output)
        qa.assert_called_once_with(output, "لماذا؟\nلأننا نختبر.")

    def test_install_voice_mesh_patches_cloud_and_local_names(self) -> None:
        original_cloud = getattr(voice_mesh.orchestrator, "synthesize_wav", None)
        original_local = getattr(voice_mesh.orchestrator, "synthesize_local_wav", None)
        try:
            voice_mesh.install_voice_mesh()
            self.assertIs(voice_mesh.orchestrator.synthesize_wav, voice_mesh.synthesize)
            self.assertIs(voice_mesh.orchestrator.synthesize_local_wav, voice_mesh.synthesize_local_wav)
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

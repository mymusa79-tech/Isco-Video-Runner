from __future__ import annotations

import inspect
import unittest
from pathlib import Path
from unittest.mock import patch

import isco_video_agent.orchestrator as orchestrator
from scripts import provider_retry_ownership as ownership
from scripts import short_voice_v2
from scripts import voice_mesh


class ProviderRetryOwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_cloud = orchestrator.synthesize_wav
        self.original_local = orchestrator.synthesize_local_wav

    def tearDown(self) -> None:
        orchestrator.synthesize_wav = self.original_cloud
        orchestrator.synthesize_local_wav = self.original_local

    def test_voice_mesh_default_is_one_provider_attempt(self) -> None:
        parameter = inspect.signature(voice_mesh.synthesize).parameters["attempts"]
        self.assertEqual(parameter.default, 1)

    def test_voice_mesh_rejects_caller_controlled_multi_attempt_retry(self) -> None:
        with patch.object(voice_mesh, "gemini_synthesize") as gemini:
            with self.assertRaisesRegex(RuntimeError, "voice_mesh_retry_owner_violation"):
                voice_mesh.synthesize(
                    "key",
                    "نص",
                    Path("unused.wav"),
                    model="gemini-3.1-flash-tts-preview",
                    voice="Gacrux",
                    attempts=2,
                )
        gemini.assert_not_called()

    def test_install_certifies_final_tts_and_vision_boundaries_without_provider_calls(self) -> None:
        with patch.object(voice_mesh, "_local_voice") as local_voice, \
                patch.object(voice_mesh, "gemini_synthesize") as gemini:
            voice_mesh.install_voice_mesh()
        local_voice.assert_not_called()
        gemini.assert_not_called()
        result = ownership.certify_provider_retry_ownership()
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["provider_calls_executed"], 0)
        self.assertEqual(result["vision_single_wire_boundaries"], 3)

    def test_short_voice_uses_engine_tts_owner_not_direct_gemini_boundary(self) -> None:
        source = inspect.getsource(short_voice_v2.apply_short_voice_v2)
        self.assertIn("orchestrator._synthesize_tts_section", source)
        self.assertNotIn("gemini_synthesize", source)

    def test_engine_runner_tts_path_forces_literal_single_attempt(self) -> None:
        self.assertGreaterEqual(
            ownership._literal_attempts_one_calls(
                orchestrator._synthesize_tts_section, "synthesize_wav"
            ),
            1,
        )


if __name__ == "__main__":
    unittest.main()

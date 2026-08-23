from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import audio_mastering_live_binding as binding


class AudioMasteringLiveBindingTests(unittest.TestCase):
    def test_canonical_narration_is_mastered_and_reported(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            def original_concat(_inputs, output):
                path = Path(output)
                path.write_bytes(b"raw-narration")
                return path

            def fake_master(src, dest):
                self.assertEqual(Path(src).name, "narration.wav")
                Path(dest).write_bytes(b"mastered-narration")
                return Path(dest)

            with patch.object(binding.orchestrator, "concat_audio", original_concat), patch.object(
                binding, "master_narration_lite", side_effect=fake_master
            ) as master, patch.object(
                binding,
                "mastering_report",
                return_value={
                    "production_profile_frozen": True,
                    "loudness_authority": "existing_mux_two_pass_loudnorm_and_limiter",
                    "zero_additional_ai_calls": True,
                },
            ):
                with binding.audio_mastering_scope():
                    result = binding.orchestrator.concat_audio([], root / "narration.wav")
                self.assertIs(binding.orchestrator.concat_audio, original_concat)

            master.assert_called_once()
            self.assertEqual(Path(result), root / "narration-mastered.wav")
            self.assertEqual(Path(result).read_bytes(), b"mastered-narration")
            report = json.loads((root / "audio-mastering.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "applied")
            self.assertEqual(report["production_stage"], "post_concat_pre_sfx_pre_mux")
            self.assertEqual(report["source"], "narration.wav")
            self.assertEqual(report["output"], "narration-mastered.wav")
            self.assertTrue(report["zero_additional_ai_calls"])

    def test_noncanonical_audio_concat_passes_through_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            def original_concat(_inputs, output):
                path = Path(output)
                path.write_bytes(b"other")
                return path

            with patch.object(binding.orchestrator, "concat_audio", original_concat), patch.object(
                binding, "master_narration_lite"
            ) as master:
                with binding.audio_mastering_scope():
                    result = binding.orchestrator.concat_audio([], root / "other.wav")

            self.assertEqual(Path(result), root / "other.wav")
            master.assert_not_called()
            self.assertFalse((root / "audio-mastering.json").exists())

    def test_mastering_failure_is_fail_closed_and_hook_is_restored(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            def original_concat(_inputs, output):
                path = Path(output)
                path.write_bytes(b"raw")
                return path

            with patch.object(binding.orchestrator, "concat_audio", original_concat), patch.object(
                binding, "master_narration_lite", side_effect=RuntimeError("synthetic mastering failure")
            ):
                with self.assertRaisesRegex(RuntimeError, "synthetic mastering failure"):
                    with binding.audio_mastering_scope():
                        binding.orchestrator.concat_audio([], root / "narration.wav")
                self.assertIs(binding.orchestrator.concat_audio, original_concat)

    def test_installer_wraps_produce_once(self) -> None:
        def base_produce(*_args, **_kwargs):
            return Path("output/example")

        original = binding.orchestrator.produce
        try:
            binding.orchestrator.produce = base_produce
            binding.install_audio_mastering_live_binding()
            first = binding.orchestrator.produce
            self.assertTrue(getattr(first, "_isco_audio_mastering_live_binding", False))
            binding.install_audio_mastering_live_binding()
            self.assertIs(binding.orchestrator.produce, first)
        finally:
            binding.orchestrator.produce = original


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import isco_video_agent.orchestrator as orchestrator
import scripts.cinematic_audio_mastering_runtime as runtime
import scripts.m7_live_binding as m7_binding


class AudioMasteringLiveBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_mux = orchestrator.mux
        self.original_produce = orchestrator.produce

    def tearDown(self) -> None:
        orchestrator.mux = self.original_mux
        orchestrator.produce = self.original_produce

    def test_installer_is_idempotent(self) -> None:
        def fake_mux(*args, **kwargs):
            return Path(args[2])

        orchestrator.mux = fake_mux
        runtime.install_audio_mastering_runtime()
        first = orchestrator.mux
        runtime.install_audio_mastering_runtime()
        self.assertIs(orchestrator.mux, first)
        self.assertTrue(getattr(first, runtime._MARKER, False))

    def test_m7_production_activation_reaches_audio_mastering_installer(self) -> None:
        def fake_produce(*args, **kwargs):
            return Path("output")

        orchestrator.produce = fake_produce
        with patch.object(m7_binding, "install_audio_mastering_runtime") as install_audio:
            with patch.object(m7_binding, "install_security_v1_live_binding"):
                m7_binding.install_m7_live_binding()
        install_audio.assert_called_once_with()
        self.assertTrue(getattr(orchestrator.produce, "_isco_m7_live_binding", False))

    def test_mastered_audio_is_passed_to_existing_mux_and_report_is_written(self) -> None:
        calls: list[tuple] = []

        def fake_mux(video, audio, output, *args, **kwargs):
            calls.append((Path(video), Path(audio), Path(output), args, kwargs))
            return Path(output)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "narration.wav"
            source.write_bytes(b"source")
            video = root / "video.mp4"
            video.write_bytes(b"video")
            output = root / "final.mp4"
            orchestrator.mux = fake_mux

            def fake_master(src: Path, dest: Path):
                self.assertEqual(Path(src), source)
                Path(dest).write_bytes(b"mastered")
                return Path(dest)

            with patch.object(runtime, "master_narration_lite", side_effect=fake_master):
                with patch.object(
                    runtime,
                    "mastering_report",
                    return_value={
                        "schema_version": 1,
                        "profile_version": "audio-mastering-lite-charon-v1",
                        "loudness_authority": "existing_mux_two_pass_loudnorm_and_limiter",
                        "zero_additional_ai_calls": True,
                    },
                ):
                    runtime.install_audio_mastering_runtime()
                    result = orchestrator.mux(video, source, output, music="music.mp3", target_lufs=-14.0)

            self.assertEqual(result, output)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][1], root / "narration-mastered.wav")
            self.assertEqual(calls[0][4]["music"], "music.mp3")
            self.assertEqual(calls[0][4]["target_lufs"], -14.0)
            report = json.loads((root / "audio-mastering.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "applied")
            self.assertEqual(report["source_audio"], "narration.wav")
            self.assertEqual(report["mastered_audio"], "narration-mastered.wav")
            self.assertEqual(report["placement"], "after_narration_concat_before_final_mux")
            self.assertTrue(report["final_loudness_authority_unchanged"])
            self.assertTrue(report["zero_additional_ai_calls"])

    def test_mastering_failure_is_fail_closed_before_existing_mux(self) -> None:
        called = False

        def fake_mux(*args, **kwargs):
            nonlocal called
            called = True
            return Path(args[2])

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "narration.wav"
            source.write_bytes(b"source")
            video = root / "video.mp4"
            video.write_bytes(b"video")
            output = root / "final.mp4"
            orchestrator.mux = fake_mux
            with patch.object(runtime, "master_narration_lite", side_effect=RuntimeError("mastering failed")):
                runtime.install_audio_mastering_runtime()
                with self.assertRaisesRegex(RuntimeError, "mastering failed"):
                    orchestrator.mux(video, source, output)
            self.assertFalse(called)
            self.assertFalse((root / "audio-mastering.json").exists())


if __name__ == "__main__":
    unittest.main()

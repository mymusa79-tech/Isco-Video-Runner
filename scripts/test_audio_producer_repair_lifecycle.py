from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import isco_video_agent.orchestrator as orchestrator
from scripts import audio_producer_repair_lifecycle as audio_lifecycle
from scripts import short_voice_v2


GOOD = {
    "integrated_lufs": -16.0,
    "true_peak_dbtp": -1.5,
    "lra": 3.0,
    "threshold": -26.0,
}
REPAIRABLE = {
    "integrated_lufs": -19.0,
    "true_peak_dbtp": -2.0,
    "lra": 3.0,
    "threshold": -29.0,
}


def _state(*, audio_streams: int = 1, av_delta: float | None = 0.02) -> dict:
    return {
        "video_streams": 1,
        "audio_streams": audio_streams,
        "video_seconds": 15.0,
        "audio_seconds": 14.98 if audio_streams else 0.0,
        "av_delta_seconds": av_delta,
        "video_codec": "h264",
    }


class AudioProducerLifecycleTests(unittest.TestCase):
    def _final(self, root: Path) -> Path:
        path = root / "final.mp4"
        path.write_bytes(b"x" * 2048)
        return path

    def test_clean_audio_passes_without_repair(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            final = self._final(root)
            repair = Mock()
            receipt = audio_lifecycle.resolve_audio_producer_handoff(
                final,
                phase="core_mux",
                target_lufs=-16.0,
                expected_audio=True,
                measure_fn=lambda _path: dict(GOOD),
                state_fn=lambda _path: _state(),
                repair_fn=repair,
            )
            self.assertEqual(receipt["decision"], "pass")
            self.assertEqual(receipt["repair_attempts"], 0)
            repair.assert_not_called()
            report = json.loads((root / audio_lifecycle.REPORT_FILENAME).read_text(encoding="utf-8"))
            self.assertEqual(report["receipts"][0]["phase"], "core_mux")

    def test_repairable_loudness_gets_exactly_one_owned_repair_then_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            final = self._final(root)
            calls = {"n": 0}

            def repair(path: Path, target: float):
                calls["n"] += 1
                self.assertEqual(path, final)
                self.assertEqual(target, -16.0)
                return path, {"measurement": dict(GOOD), "media_state": _state()}

            receipt = audio_lifecycle.resolve_audio_producer_handoff(
                final,
                phase="core_mux",
                target_lufs=-16.0,
                expected_audio=True,
                measure_fn=lambda _path: dict(REPAIRABLE),
                state_fn=lambda _path: _state(),
                repair_fn=repair,
            )
            self.assertEqual(calls["n"], 1)
            self.assertEqual(receipt["decision"], "repaired_pass")
            self.assertEqual(receipt["repair_attempts"], 1)
            self.assertTrue(receipt["independent_gate_still_required"])

    def test_still_invalid_after_one_repair_fails_closed_without_second_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            final = self._final(Path(temp_dir))
            calls = {"n": 0}

            def repair(path: Path, target: float):
                calls["n"] += 1
                return path, {"measurement": dict(REPAIRABLE), "media_state": _state()}

            with self.assertRaisesRegex(
                audio_lifecycle.AudioProducerRepairError,
                "repair_revalidation_failed",
            ):
                audio_lifecycle.resolve_audio_producer_handoff(
                    final,
                    phase="core_mux",
                    target_lufs=-16.0,
                    expected_audio=True,
                    measure_fn=lambda _path: dict(REPAIRABLE),
                    state_fn=lambda _path: _state(),
                    repair_fn=repair,
                )
            self.assertEqual(calls["n"], 1)

    def test_av_sync_is_not_silently_repaired(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            final = self._final(Path(temp_dir))
            repair = Mock()
            with self.assertRaisesRegex(
                audio_lifecycle.AudioProducerRepairError,
                "av_sync_unowned_fail_closed",
            ):
                audio_lifecycle.resolve_audio_producer_handoff(
                    final,
                    phase="core_mux",
                    target_lufs=-16.0,
                    expected_audio=True,
                    measure_fn=lambda _path: dict(REPAIRABLE),
                    state_fn=lambda _path: _state(av_delta=2.0),
                    repair_fn=repair,
                )
            repair.assert_not_called()

    def test_clipped_or_extreme_audio_is_outside_repair_allowlist(self) -> None:
        bad_peak = dict(GOOD)
        bad_peak["true_peak_dbtp"] = 0.2
        with tempfile.TemporaryDirectory() as temp_dir:
            final = self._final(Path(temp_dir))
            repair = Mock()
            with self.assertRaisesRegex(
                audio_lifecycle.AudioProducerRepairError,
                "loudness_unowned_fail_closed",
            ):
                audio_lifecycle.resolve_audio_producer_handoff(
                    final,
                    phase="core_mux",
                    target_lufs=-16.0,
                    expected_audio=True,
                    measure_fn=lambda _path: bad_peak,
                    state_fn=lambda _path: _state(),
                    repair_fn=repair,
                )
            repair.assert_not_called()

    def test_audio_not_required_remains_not_applicable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            final = self._final(Path(temp_dir))
            receipt = audio_lifecycle.resolve_audio_producer_handoff(
                final,
                phase="core_mux",
                target_lufs=-18.0,
                expected_audio=False,
                measure_fn=Mock(side_effect=AssertionError("must not measure")),
                state_fn=lambda _path: _state(audio_streams=0, av_delta=None),
                repair_fn=Mock(side_effect=AssertionError("must not repair")),
            )
            self.assertEqual(receipt["decision"], "not_applicable")

    def test_core_mux_wrapper_uses_long_minus16_and_music_only_minus18(self) -> None:
        original = orchestrator.mux
        calls: list[tuple[str, float, bool]] = []
        try:
            def fake_mux(video, narration, output, *args, **kwargs):
                Path(output).write_bytes(b"x" * 2048)
                return Path(output)

            orchestrator.mux = fake_mux
            with patch.object(
                audio_lifecycle,
                "resolve_audio_producer_handoff",
                side_effect=lambda _path, *, phase, target_lufs, expected_audio, **_kwargs: calls.append(
                    (phase, target_lufs, expected_audio)
                ) or {},
            ):
                audio_lifecycle._install_core_mux_wrapper()
                with tempfile.TemporaryDirectory() as temp_dir:
                    root = Path(temp_dir)
                    orchestrator.mux(Path("v.mp4"), Path("n.wav"), root / "long.mp4", target_lufs=-16.0)
                    orchestrator.mux(Path("v.mp4"), None, root / "moment.mp4", music=Path("m.wav"))
                    orchestrator.mux(Path("v.mp4"), None, root / "silent.mp4")
            self.assertEqual(
                calls,
                [
                    ("core_mux", -16.0, True),
                    ("core_mux", -18.0, True),
                    ("core_mux", -18.0, False),
                ],
            )
        finally:
            orchestrator.mux = original

    def test_finished_short_precheck_runs_before_independent_refresh(self) -> None:
        original = short_voice_v2._refresh_quality_final
        events: list[str] = []
        try:
            short_voice_v2._refresh_quality_final = lambda root, final: events.append("independent") or {"audio_ok": True}
            with patch.object(
                audio_lifecycle,
                "resolve_audio_producer_handoff",
                side_effect=lambda *args, **kwargs: events.append("producer") or {},
            ):
                audio_lifecycle._install_short_finished_wrapper()
                short_voice_v2._refresh_quality_final(Path("out"), Path("out/final.mp4"))
            self.assertEqual(events, ["producer", "independent"])
        finally:
            short_voice_v2._refresh_quality_final = original

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
    def test_real_ffmpeg_correction_preserves_video_stream_and_reaches_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            final = root / "final.mp4"
            subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=c=black:s=320x240:r=25:d=2",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                    "-filter:a", "volume=0.2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(final),
                ],
                check=True,
            )
            before_hash = audio_lifecycle._video_stream_hash(final)
            _path, evidence = audio_lifecycle._render_corrected_candidate(final, -16.0)
            after_hash = audio_lifecycle._video_stream_hash(final)
            self.assertEqual(before_hash, after_hash)
            self.assertTrue(audio_lifecycle._measurement_passes(evidence["measurement"], -16.0))
            self.assertLessEqual(
                abs(evidence["media_state"]["video_seconds"] - evidence["media_state"]["audio_seconds"]),
                1.0,
            )


if __name__ == "__main__":
    unittest.main()

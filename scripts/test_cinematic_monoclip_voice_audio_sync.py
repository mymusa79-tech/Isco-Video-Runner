"""Offline real-FFmpeg Monoclip audiovisual integration (NO external TTS/API calls).

Uses an audible generated carrier solely to verify that a voice-owned audio
track survives media render -> identity bypass -> large Cairo captions, including
the last 250 ms. It does NOT simulate human speech or certify Iapetus quality.
"""
from __future__ import annotations

import json
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clean_v2.media import render_video, probe_duration
from clean_v2.short_timed_text import apply_short_timed_text, _clean
from clean_v2.timeline_render import render_identity_composition
from scripts.test_cinematic_monoclip_real_ffmpeg import (
    CinematicMonoclipActualRenderTests,
    run,
)


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe") and shutil.which("fc-match"),
    "FFmpeg, FFprobe and fontconfig required",
)
class CinematicMonoclipNarrationAndCaptionsTests(unittest.TestCase):
    def _last_audio_is_not_muted(self, movie: Path, *, end: float) -> None:
        decoded = run(
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-ss", f"{end-0.35:.3f}", "-t", "0.22",
            "-i", str(movie), "-vn", "-ac", "1", "-ar", "16000",
            "-f", "s16le", "-",
        ).stdout
        self.assertGreater(len(decoded), 2000)
        values = struct.unpack("<" + "h" * (len(decoded) // 2), decoded)
        self.assertGreater(max(abs(x) for x in values), 250)

    def test_real_audio_video_color_bw_with_full_last_caption(self):
        helper = CinematicMonoclipActualRenderTests()
        for style in ("color_cinematic", "bw_cinematic"):
            with self.subTest(style=style), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                narration, scenes = helper._fixture(root, style)
                # Replace anullsrc with *audible* local PCM without API cost.
                run(
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                    "-t", "4.1", "-c:a", "pcm_s16le", str(narration),
                )
                doc = json.loads((root / "production.json").read_text())
                self.assertEqual(doc["visual_style"], style)
                picture = render_video(
                    narration, scenes, root / "body-with-audio.mp4", fmt="short",
                )
                self.assertAlmostEqual(probe_duration(picture), 4.1, delta=0.18)
                self._last_audio_is_not_muted(picture, end=4.1)

                # The prayer/outro-free identity compositor must be byte-identical.
                timeline = json.loads((root / "timeline-first.json").read_text())
                self.assertTrue(timeline["cinematic_monoclip"])
                self.assertFalse(any(
                    x["kind"] in {"prayer", "outro", "final_silence"}
                    for x in timeline["identity_events"]
                ))
                final = root / "final.mp4"
                render_identity_composition(
                    picture, final, fmt="short", timeline=timeline,
                )
                self.assertEqual(final.read_bytes(), picture.read_bytes())

                spoken = [
                    {"start": 0.0, "end": 1.1, "text": "قد تمر بجمال الطريق دون أن تراه."},
                    {"start": 1.3, "end": 3.0, "text": "عيناك هنا ولكن انتباهك مشغول بما سيأتي."},
                    {"start": 3.0, "end": 4.1, "text": "انتبه لهذه اللحظة قبل أن تمر."},
                ]
                with mock.patch(
                    "clean_v2.podcast_captions.build_events",
                    return_value=(spoken, {"source": "offline_audio_fixture"}),
                ):
                    report = apply_short_timed_text(
                        output_dir=root, final_path=final, narration_path=narration,
                        script={"sections": []},
                    )
                self.assertIsInstance(report, dict)
                self.assertAlmostEqual(probe_duration(final), 4.1, delta=0.18)
                self._last_audio_is_not_muted(final, end=4.1)
                srt = (root / "short-timed-text.srt").read_text("utf-8")
                words = " ".join(e["text"] for e in spoken)
                self.assertEqual(
                    _clean(" ".join(
                        line for line in srt.splitlines()
                        if line.strip() and "-->" not in line and not line.strip().isdigit()
                    )), _clean(words),
                )
                self.assertIn("تمر.", srt)
                self.assertTrue(final.stat().st_size > 10_000)
                self.assertTrue(report["status"] == "pass")
                # Captions were burned by the *actual* production renderer.
                self.assertGreater(int(report.get("event_count", 0)), 1)

if __name__ == "__main__":
    unittest.main()

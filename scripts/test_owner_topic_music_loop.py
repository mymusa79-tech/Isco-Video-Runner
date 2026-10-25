"""Regression checks: an approved YouTube bed repeats gently without masking speech.

Run: python -m unittest scripts.test_owner_topic_music_loop
Requires local ffmpeg and ffprobe; no provider calls and no voice quota.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from clean_v2 import short_audio_polish as audio


def _cmd(args: list[str]) -> None:
    subprocess.run(args, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _length(path: Path) -> float:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    )
    return float(json.loads(proc.stdout)["format"]["duration"])


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg not installed")
class ApprovedTopicMusicLoopTests(unittest.TestCase):
    def test_crossfaded_owner_music_fills_the_full_topic(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            directory = Path(scratch)
            source, output = directory / "approved.wav", directory / "repeated.wav"
            _cmd([
                "ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                "-i", "sine=frequency=440:sample_rate=48000:duration=10",
                "-ac", "1", "-c:a", "pcm_s16le", str(source),
            ])
            audio._prepare_local_music_bed(
                source, output, 24.0, seamless_repeat=True, source_duration=10.0
            )
            self.assertAlmostEqual(_length(output), 24.0, delta=0.06)

    def test_ducked_music_mixes_without_changing_video_duration(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            directory = Path(scratch)
            voice_video = directory / "voice.mp4"
            music = directory / "music.wav"
            output = directory / "out.mp4"
            _cmd([
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=black:s=160x90:r=10:d=4",
                "-f", "lavfi", "-i", "sine=frequency=500:sample_rate=48000:duration=4",
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-shortest", str(voice_video),
            ])
            _cmd([
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=6",
                "-ac", "1", "-c:a", "pcm_s16le", str(music),
            ])
            audio._mix_music_into_video(
                final_path=voice_video,
                output_path=output,
                music_path=music,
                topic_start_seconds=0.5,
                duck_under_voice=True,
            )
            self.assertTrue(output.is_file())
            self.assertAlmostEqual(_length(output), _length(voice_video), delta=0.12)

    def test_topic_window_excludes_identity(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            directory = Path(scratch)
            (directory / "timeline-first.json").write_text(json.dumps({
                "identity_events": [
                    {"kind": "hook", "start": 0, "end": 9},
                    {"kind": "intro", "start": 9, "end": 15},
                    {"kind": "prayer", "start": 15, "end": 21},
                    {"kind": "topic", "start": 21, "end": 200},
                    {"kind": "outro", "start": 200, "end": 206},
                ]
            }), encoding="utf-8")
            self.assertEqual(audio._topic_window(directory), (21.0, 200.0))
            self.assertGreater(
                200.0 - (21.0 + 3.0) - audio.OWNER_MUSIC_OUTRO_CLEARANCE_SECONDS, 0
            )


if __name__ == "__main__":
    unittest.main()

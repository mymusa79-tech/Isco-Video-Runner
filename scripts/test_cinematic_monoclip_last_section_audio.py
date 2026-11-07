"""Regression: the Monoclip narration must contain EVERY section, including the last one.

Production run 42322175011 delivered a Short whose voice stopped after section 2: the last
section lives in audio/NN-chunks/ (identity section, one chunk, no final silence in Monoclip),
was never copied to audio/NN.wav, and ffmpeg's concat demuxer silently skipped the missing file.
Offline: the fake voice is a generated tone, no TTS or network is used.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from clean_v2.media import concat_wav_parts, probe_duration


def _tone(path: Path, seconds: float) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
         f"sine=frequency=220:sample_rate=24000:duration={seconds}", "-ac", "1", str(path)],
        check=True,
    )


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg and FFprobe required")
class MonoclipLastSectionAudioTests(unittest.TestCase):
    def test_concat_refuses_a_missing_part_instead_of_dropping_it(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _tone(root / "a.wav", 1.0)
            _tone(root / "b.wav", 1.0)
            with self.assertRaises(FileNotFoundError):
                concat_wav_parts([root / "a.wav", root / "b.wav", root / "missing.wav"], root / "out.wav")

    def test_monoclip_narration_keeps_the_last_section(self):
        from clean_v2.pipeline import _synthesize_sectioned_voice
        from scripts.test_clean_v2 import _FakeVoice

        sections = [
            {"id": "s1", "narration": "تمر الفرشاة على الورق مرة أخرى. متى يتحول الإتقان إلى فخ؟"},
            {"id": "s2", "narration": "حين تصقل الطبقة الأولى قبل أن تظهر الصورة لا تتقدم."},
            {"id": "s3", "narration": "قبل أن تعيد لمسة اسأل هل تقربني من اكتمال الصورة."},
        ]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "production.json").write_text(json.dumps({"visual_style": "color_cinematic"}), encoding="utf-8")
            _synthesize_sectioned_voice(_FakeVoice(), sections, root / "narration.wav", fmt="short")
            parts = [root / "audio" / f"{i:02d}.wav" for i in (1, 2, 3)]
            for part in parts:
                self.assertTrue(part.is_file(), f"missing section file {part.name}")
            expected = sum(probe_duration(part) for part in parts)
            self.assertAlmostEqual(probe_duration(root / "narration.wav"), expected, delta=0.08)
            first_two = probe_duration(parts[0]) + probe_duration(parts[1])
            self.assertGreater(probe_duration(root / "narration.wav"), first_two + 0.3)


if __name__ == "__main__":
    unittest.main()

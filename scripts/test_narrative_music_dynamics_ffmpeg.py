from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from isco_video_agent.security import secret_free_subprocess_env
from scripts import narrative_music_dynamics as dynamics


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe required")
class NarrativeMusicDynamicsFfmpegTests(unittest.TestCase):
    def test_real_ffmpeg_renders_time_varying_music_bed_to_exact_duration(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "music.wav"
            dest = root / "dynamic.wav"
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=220:sample_rate=48000:duration=1.0",
                    "-c:a",
                    "pcm_s16le",
                    str(source),
                ],
                check=True,
                env=secret_free_subprocess_env(),
            )
            schedule = [
                {"start_seconds": 0.0, "end_seconds": 1.5, "adjustment_db": -0.8},
                {"start_seconds": 1.5, "end_seconds": 3.0, "adjustment_db": 1.8},
                {"start_seconds": 3.0, "end_seconds": 4.0, "adjustment_db": dynamics.OUTRO_ADJUSTMENT_DB},
            ]
            expression = dynamics._ffmpeg_volume_expression(dynamics._ramp_pieces(schedule))
            dynamics._render_dynamic_music(source, dest, total_seconds=4.0, expression=expression)
            self.assertTrue(dest.is_file())
            self.assertGreater(dest.stat().st_size, 1024)
            self.assertAlmostEqual(dynamics.duration(dest), 4.0, delta=0.10)


if __name__ == "__main__":
    unittest.main()

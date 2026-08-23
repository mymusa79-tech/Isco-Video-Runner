from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from isco_video_agent.security import secret_free_subprocess_env
from scripts import final_master_qc as qc


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe required")
class FinalMasterQCFfmpegTests(unittest.TestCase):
    def test_real_full_decode_and_detector_scan_on_clean_av_master(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            final = root / "clean.mp4"
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
                    "testsrc2=size=320x180:rate=30:duration=3",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:sample_rate=48000:duration=3",
                    "-map",
                    "0:v:0",
                    "-map",
                    "1:a:0",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-ar",
                    "48000",
                    "-shortest",
                    str(final),
                ],
                check=True,
                env=secret_free_subprocess_env(),
            )
            scan = qc._run_full_scan(final, has_audio=True)
            self.assertEqual(scan["returncode"], 0)
            self.assertEqual(scan["black_events"], [])
            stderr = scan["stderr"]
            self.assertEqual(qc._parse_silence_events(stderr, final_seconds=3.0), [])
            self.assertEqual(qc._parse_freeze_events(stderr, final_seconds=3.0), [])


if __name__ == "__main__":
    unittest.main()

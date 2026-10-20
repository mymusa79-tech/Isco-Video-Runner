"""One gentle look per format over every scene clip; boards and failures are left alone."""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clean_v2 import owner_art  # noqa: E402
from clean_v2.media import probe_duration  # noqa: E402

FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def mean_rgb(path: Path):
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-frames:v", "1", "-vf", "scale=1:1,format=rgb24",
                          "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
    return tuple(out[:3])


class ProfileTests(unittest.TestCase):
    def test_profile_choice(self):
        self.assertEqual(owner_art.grade_profile("dialogue_qa", "podcast"), "podcast")
        self.assertEqual(owner_art.grade_profile("ala_mahl", "short"), "ala_mahl")
        self.assertEqual(owner_art.grade_profile("why_reframe", "short"), "short")
        self.assertEqual(owner_art.grade_profile("direct_cinematic", "film"), "film")
        self.assertEqual(set(owner_art.GRADE_PROFILES), {"podcast", "film", "short", "ala_mahl"})


@unittest.skipUnless(FFMPEG, "ffmpeg required")
class GradeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def clip(self, name, colour):
        path = self.root / name
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c={colour}:s=320x180:r=30:d=2",
                        "-pix_fmt", "yuv420p", str(path)], check=True)
        return path

    def test_grade_keeps_duration_and_changes_colour(self):
        clip = self.clip("a.mp4", "0xE04020")
        before, length = mean_rgb(clip), probe_duration(clip)
        self.assertEqual(owner_art.grade_clip(clip, "podcast"), "podcast")
        self.assertNotEqual(mean_rgb(clip), before)
        self.assertAlmostEqual(probe_duration(clip), length, delta=0.2)

    def test_formats_get_different_looks(self):
        a, b = self.clip("a.mp4", "0xC0A080"), self.clip("b.mp4", "0xC0A080")
        owner_art.grade_clip(a, "podcast")
        owner_art.grade_clip(b, "ala_mahl")
        self.assertNotEqual(mean_rgb(a), mean_rgb(b))

    def test_failure_is_fail_soft(self):
        missing = self.root / "none.mp4"
        self.assertTrue(owner_art.grade_clip(missing, "film").startswith("unavailable:"))
        self.assertFalse(list(self.root.glob("*.graded.mp4")))


if __name__ == "__main__":
    unittest.main()

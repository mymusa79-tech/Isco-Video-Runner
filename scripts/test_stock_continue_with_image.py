from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clean_v2.media import probe_duration
from clean_v2.owner_art import ART_DIR_ENV
from clean_v2.simple_mode import (
    STOCK_CONTINUE_MIN_SECONDS,
    _continue_short_clip_with_image,
    stock_outcomes,
)


def _make_clip(path: Path, seconds: float) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", f"testsrc2=size=1280x720:rate=30:duration={seconds}",
         "-pix_fmt", "yuv420p", str(path)],
        check=True, timeout=120,
    )


def _make_image(path: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "color=c=0x2a3b4c:size=1920x1080", "-frames:v", "1", str(path)],
        check=True, timeout=60,
    )


class StockContinueWithImageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.art = self.root / "art"
        self.art.mkdir()
        _make_image(self.art / "image-01.jpg")
        self.clip = self.root / "visual-scene01.mp4"
        self.row = {"stock_requested": True, "approved_fallback_asset": "image-01.jpg",
                    "source_actual": "stock_motion"}
        patcher = mock.patch.dict(os.environ, {ART_DIR_ENV: str(self.art)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def test_short_clip_plays_then_approved_image_finishes_the_slot(self) -> None:
        _make_clip(self.clip, 4.0)
        done = _continue_short_clip_with_image(self.clip, self.row, {"owner_motion": "slow_pan"},
                                               index=0, fmt="film", seconds=12.0)
        self.assertTrue(done)
        self.assertGreaterEqual(probe_duration(self.clip), 11.8)
        self.assertTrue(self.row["stock_continues_with_image"])
        self.assertEqual(self.row["stock_source_seconds"], 4.0)
        self.assertEqual(self.row["source_actual"], "stock_motion")
        self.assertEqual([p.name for p in self.root.iterdir() if "tail" in p.name or "joined" in p.name], [])

    def test_clip_that_already_covers_the_slot_is_untouched(self) -> None:
        _make_clip(self.clip, 6.0)
        before = self.clip.read_bytes()
        self.assertFalse(_continue_short_clip_with_image(self.clip, self.row, {}, index=0, fmt="film", seconds=6.0))
        self.assertEqual(self.clip.read_bytes(), before)
        self.assertNotIn("stock_continues_with_image", self.row)

    def test_clip_below_minimum_is_not_worth_a_scene(self) -> None:
        _make_clip(self.clip, STOCK_CONTINUE_MIN_SECONDS - 0.5)
        self.assertFalse(_continue_short_clip_with_image(self.clip, self.row, {}, index=0, fmt="film", seconds=10.0))

    def test_no_approved_image_means_no_continuation(self) -> None:
        _make_clip(self.clip, 4.0)
        row = {"stock_requested": True, "source_actual": "stock_motion"}
        self.assertFalse(_continue_short_clip_with_image(self.clip, row, {}, index=0, fmt="film", seconds=10.0))
        row = {**self.row, "approved_fallback_asset": "missing.jpg"}
        self.assertFalse(_continue_short_clip_with_image(self.clip, row, {}, index=0, fmt="film", seconds=10.0))

    def test_continued_scene_counts_as_stock_used(self) -> None:
        import json
        rows = [{"beat_id": "scene01", "stock_requested": True, "source_actual": "stock_motion",
                 "stock_continues_with_image": True}]
        (self.root / "rights-manifest.json").write_text(json.dumps({"assets": rows}), encoding="utf-8")
        outcome = stock_outcomes(self.root)
        self.assertEqual(outcome["used"], 1)
        self.assertEqual(outcome["continued_with_approved_image"], 1)

    def test_portrait_continuation_is_vertical(self) -> None:
        _make_clip(self.clip, 3.0)
        # portrait follows the same format set as the renderer (Short arrives as "story")
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                        "-i", "testsrc2=size=720x1280:rate=30:duration=3", "-pix_fmt", "yuv420p", str(self.clip)],
                       check=True, timeout=120)
        self.assertTrue(_continue_short_clip_with_image(self.clip, self.row, {}, index=1, fmt="story", seconds=8.0))
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "stream=width,height", "-of", "csv=p=0", str(self.clip)],
                             check=True, capture_output=True, text=True).stdout.strip()
        self.assertEqual(out, "720,1280")


if __name__ == "__main__":
    unittest.main()

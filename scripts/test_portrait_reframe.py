"""Derived Shorts fill the portrait screen instead of sitting as a strip inside a blurred canvas."""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from clean_v2 import portrait_reframe as pr
from clean_v2.media import render_derived_short


def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height", "-show_entries", "format=duration",
         "-of", "json", str(path)], check=True, capture_output=True, text=True).stdout
    return json.loads(out)


class ReframeMathTests(unittest.TestCase):
    def test_window_is_three_by_four_and_sits_centred_in_the_canvas(self):
        self.assertEqual((pr.WINDOW_WIDTH, pr.WINDOW_HEIGHT), (1080, 1440))
        self.assertEqual(pr.WINDOW_TOP, 240)
        self.assertEqual(pr.WINDOW_TOP * 2 + pr.WINDOW_HEIGHT, pr.CANVAS_HEIGHT)

    def test_flat_or_centred_detail_keeps_the_window_centred(self):
        self.assertEqual(pr.best_fraction([1.0] * 320), 0.5)
        self.assertEqual(pr.best_fraction([0.0] * 320), 0.5)
        centred = [0.0] * 320
        centred[110:210] = [5.0] * 100
        self.assertEqual(pr.best_fraction(centred), 0.5)

    def test_content_far_to_one_side_moves_the_window_but_never_past_the_limit(self):
        left = [0.0] * 320
        left[10:70] = [9.0] * 60
        fraction = pr.best_fraction(left)
        self.assertLess(fraction, 0.5)
        self.assertGreaterEqual(fraction, pr.FOCUS_MIN)
        right = list(reversed(left))
        self.assertGreater(pr.best_fraction(right), 0.5)
        self.assertLessEqual(pr.best_fraction(right), pr.FOCUS_MAX)

    def test_focus_falls_back_to_the_centre_when_sampling_fails(self):
        def boom(*_a, **_k):
            raise OSError("no ffmpeg")

        self.assertEqual(pr.focus_fraction(Path("missing.mp4"), 0, 10, run=boom), 0.5)

    def test_filter_graph_keeps_blurred_bands_and_a_faded_picture_window(self):
        graph = pr.filter_graph(0.4)
        self.assertIn("scale=1080:1920", graph)
        self.assertIn("crop=w=ih*3/4:h=ih", graph)
        self.assertIn("(iw-ow)*0.4000", graph)
        self.assertIn("alphamerge", graph)
        self.assertIn("overlay=0:240", graph)
        self.assertIn("[v]", graph)
        self.assertIn("(iw-ow)*1.0000", pr.filter_graph(7))  # clamped
        self.assertIn("(iw-ow)*0.0000", pr.filter_graph(-1))

    def test_fade_mask_is_opaque_inside_and_transparent_at_both_edges(self):
        with tempfile.TemporaryDirectory() as tmp:
            mask = Image.open(pr.fade_mask(Path(tmp) / "m.png"))
            self.assertEqual(mask.size, (1080, 1440))
            self.assertEqual(mask.getpixel((500, 720)), 255)
            self.assertEqual(mask.getpixel((500, 0)), 0)
            self.assertEqual(mask.getpixel((500, 1439)), 0)
            self.assertTrue(0 < mask.getpixel((500, 30)) < 255)


class RealRenderTests(unittest.TestCase):
    def test_landscape_master_becomes_a_1080x1920_short_with_audio_and_exact_length(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, output = root / "master.mp4", root / "short.mp4"
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                 "testsrc2=size=1920x1080:rate=30:duration=9", "-f", "lavfi", "-i", "sine=frequency=440:duration=9",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(source)],
                check=True)
            render_derived_short(source, output, start_seconds=1.0, end_seconds=8.5, maximum_seconds=60)
            info = probe(output)
            video = next(s for s in info["streams"] if s["codec_type"] == "video")
            self.assertEqual((video["width"], video["height"]), (1080, 1920))
            self.assertTrue(any(s["codec_type"] == "audio" for s in info["streams"]))
            self.assertAlmostEqual(float(info["format"]["duration"]), 7.5, delta=0.15)
            frame = root / "f.png"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", "3", "-i", str(output),
                            "-frames:v", "1", str(frame)], check=True)
            picture = Image.open(frame).convert("RGB")
            # Sharp picture window in the middle, darkened blurred band at the very top.
            self.assertGreater(sum(picture.getpixel((540, 960))), 60)
            top = sum(sum(picture.getpixel((x, 5))) for x in range(0, 1080, 90)) / 12
            middle = sum(sum(picture.getpixel((x, 960))) for x in range(0, 1080, 90)) / 12
            self.assertLess(top, middle)


if __name__ == "__main__":
    unittest.main()

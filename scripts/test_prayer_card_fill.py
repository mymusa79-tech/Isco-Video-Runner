from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from clean_v2 import timeline_render
from clean_v2.identity_sequence import identity_asset_paths


class PrayerCardFillTests(unittest.TestCase):
    def test_chain_has_no_black_padding(self) -> None:
        chain = timeline_render._prayer_fill_chain(1920, 1080)
        self.assertIn("boxblur", chain)
        self.assertNotIn("color=black", chain)
        self.assertNotIn("pad=", chain)

    def test_long_form_renderers_use_the_fill_not_black_pad(self) -> None:
        source = Path(timeline_render.__file__).read_text(encoding="utf-8")
        self.assertEqual(source.count("_prayer_fill_chain(width, height)"), 2)  # podcast and film
        self.assertNotIn("{width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black", source)

    def test_real_card_on_16_9_has_no_black_side_bars(self) -> None:
        card = identity_asset_paths("podcast")["prayer"]
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "frame.png"
            chain = timeline_render._prayer_fill_chain(1920, 1080)
            subprocess.run([
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=black:s=1920x1080:d=1",
                "-f", "lavfi", "-i", "color=c=black:s=64x64:d=1",  # input 1 (intro) so the card is input 2 as in production
                "-loop", "1", "-framerate", "30", "-i", str(card),
                "-filter_complex", f"{chain}setsar=1,fps=30,format=rgba,trim=duration=1,setpts=PTS-STARTPTS[p];[0:v][p]overlay=0:0[v]",
                "-map", "[v]", "-frames:v", "1", str(out),
            ], check=True, timeout=60)
            with Image.open(out) as frame:
                frame = frame.convert("RGB")
                self.assertEqual(frame.size, (1920, 1080))
                for x in (5, 60, 1860, 1915):
                    self.assertGreater(sum(frame.getpixel((x, 540))) / 3, 90, f"black bar at x={x}")
                square = frame.crop((420, 0, 1500, 1080))
                with Image.open(card) as original:
                    reference = original.convert("RGB").resize((1080, 1080))
                diff = sum(sum(abs(a - b) for a, b in zip(p, q)) for p, q in zip(square.resize((54, 54)).getdata(), reference.resize((54, 54)).getdata())) / (54 * 54 * 3)
                self.assertLess(diff, 12)  # the card itself is not cropped or altered


if __name__ == "__main__":
    unittest.main()

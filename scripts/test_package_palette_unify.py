"""Stock clips in an approved ZIP are pulled toward the colours of the package's own pictures."""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from clean_v2 import media


def _clip(path: Path, colour: str, noise: int) -> None:
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
        f"color=c={colour}:s=135x240:r=30:d=3,noise=alls={noise}:allf=t", "-pix_fmt", "yuv420p", str(path),
    ], check=True)


def _distance(a: media._RgbStats, b: media._RgbStats) -> float:
    return ((a.mean_r - b.mean_r) ** 2 + (a.mean_g - b.mean_g) ** 2 + (a.mean_b - b.mean_b) ** 2) ** 0.5


class PackagePaletteTests(unittest.TestCase):
    def test_only_stock_is_graded_and_it_moves_toward_the_owner_pictures(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            owner, stock = root / "owner.mp4", root / "stock.mp4"
            _clip(owner, "0x3a4668", 14)
            _clip(stock, "0xb88a58", 24)
            rows = [{"local_file": "owner.mp4", "owner_approved": True},
                    {"local_file": "stock.mp4", "source_actual": "stock_motion", "owner_approved": False}]
            filters = media._package_palette_match_filters([owner, stock], rows, fmt="short")
            self.assertEqual(set(filters), {str(stock)})
            graded = root / "graded.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(stock), "-vf", filters[str(stock)],
                            "-pix_fmt", "yuv420p", str(graded)], check=True)
            reference = media._sample_rgb_stats(owner)
            self.assertLess(_distance(media._sample_rgb_stats(graded), reference),
                            _distance(media._sample_rgb_stats(stock), reference))

    def test_nothing_changes_without_both_stock_and_owner_pictures(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            owner = root / "owner.mp4"
            _clip(owner, "0x3a4668", 14)
            only_owner = [{"local_file": "owner.mp4", "owner_approved": True}]
            self.assertEqual(media._package_palette_match_filters([owner], only_owner, fmt="short"), {})

    def test_default_matcher_is_unchanged(self):
        stats = media._RgbStats(100, 110, 120, 20, 22, 24)
        self.assertEqual(media._reference_match_filter(stats, media._channel_target_stats(), fmt="short"),
                         media._reference_match_filter(stats, media._channel_target_stats(), fmt="short",
                                                       strength_override=None, scale_limits=None))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image, features

from clean_v2.cover_studio import (
    CHANNEL_NAME,
    PROGRAM_NAME,
    TONE_PROFILE,
    render_cover_studio,
    score_cover_candidate,
)


@unittest.skipUnless(features.check_feature("raqm"), "Pillow Raqm unavailable")
class CoverStudioV2Tests(unittest.TestCase):
    def _image(self, path: Path, *, value: int, accent: bool) -> Path:
        image = Image.new("RGB", (1280, 720), (value, value, value))
        if accent:
            for x in range(120, 520):
                for y in range(150, 570):
                    image.putpixel((x, y), (35, 28, 24))
        image.save(path, quality=95)
        return path

    def test_candidate_scoring_prefers_depth_over_flat_brightness(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bright = self._image(root / "bright.jpg", value=220, accent=False)
            deep = self._image(root / "deep.jpg", value=100, accent=True)
            bright_score = score_cover_candidate(bright, fmt="film")
            deep_score = score_cover_candidate(deep, fmt="film")
            self.assertGreater(deep_score["visual_score"], bright_score["visual_score"])
            self.assertEqual(deep_score["tone_target"], TONE_PROFILE)

    def test_podcast_identity_and_depth_profile_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._image(root / "source.jpg", value=105, accent=True)
            output = root / "podcast-cover.jpg"
            report = render_cover_studio(
                source,
                output,
                text="حين تختفي الرغبة",
                fmt="podcast",
            )
            self.assertTrue(output.is_file())
            self.assertEqual((report["width"], report["height"]), (1280, 720))
            self.assertEqual(report["podcast_program"], PROGRAM_NAME)
            self.assertEqual(PROGRAM_NAME, "خارج النص")
            self.assertEqual(report["channel_name"], CHANNEL_NAME)
            self.assertEqual(CHANNEL_NAME, "نداء اليقظة")
            self.assertEqual(report["tone_profile"], "deep_neutral")

    def test_short_and_film_profiles_render_expected_sizes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._image(root / "source.jpg", value=105, accent=True)
            short = render_cover_studio(
                source,
                root / "short.jpg",
                text="خطوة واحدة تكفي",
                fmt="short",
            )
            film = render_cover_studio(
                source,
                root / "film.jpg",
                text="ابدأ قبل أن تشعر",
                fmt="film",
            )
            self.assertEqual((short["width"], short["height"]), (1080, 1920))
            self.assertEqual((film["width"], film["height"]), (1280, 720))
            self.assertIn(short["layout_family"], {"impact", "split", "stacked", "centered"})
            self.assertIn(film["layout_family"], {"impact", "split", "stacked", "centered"})


if __name__ == "__main__":
    unittest.main()

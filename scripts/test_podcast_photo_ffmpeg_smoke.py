"""Offline real-FFmpeg smoke: approved podcasts use distinct, unchanged, calm photos.

This test never calls stock providers, Gemini or a publishing workflow. Small
320x180 technical fixtures keep the CI cost bounded; this is NOT a subjective
review of any episode's actual imagery.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PIL import Image, ImageChops, ImageDraw, ImageStat

from clean_v2 import owner_art, production_package, ready_production
from scripts.test_production_package import manifest

FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def frame_at(path: Path, seconds: float = 0.0) -> Image.Image:
    raw = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{seconds:.2f}",
         "-i", str(path), "-frames:v", "1", "-f", "image2pipe",
         "-vcodec", "png", "-"],
        capture_output=True, check=True, timeout=30,
    ).stdout
    with Image.open(BytesIO(raw)) as opened:
        return opened.convert("RGB")


@unittest.skipUnless(FFMPEG, "real FFmpeg integration requires ffmpeg and ffprobe")
class ApprovedPodcastPhotosSmoke(unittest.TestCase):
    def test_four_real_still_clips_stay_distinct_and_static(self):
        doc = manifest("podcast")
        scenes = []
        for section_index, section in enumerate(doc["script"]["sections"]):
            a_text = section["turns"][0]["text"]
            b_text = section["turns"][1]["text"]
            for turn_index, anchor in enumerate((a_text, b_text)):
                k = len(scenes) + 1
                scenes.append({
                    "id": f"image_{k}", "section_id": section["id"],
                    "asset": f"assets/scene_{k:02d}.png",
                    "start_text": anchor[:min(40, len(anchor))],
                    "motion": "hold", "transition": "cut",
                })
        doc["scenes"] = scenes
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "assets").mkdir()
            colours = [(22, 41, 63), (180, 148, 102), (87, 119, 99), (144, 92, 73)]
            for i, colour in enumerate(colours, start=1):
                photo = Image.new("RGB", (320, 180), colour)
                draw = ImageDraw.Draw(photo)
                # Off-center visual feature makes accidental pan/zoom measurable.
                draw.rectangle((42, 37, 112, 104), fill=(225, 230, 216))
                photo.save(root / f"assets/scene_{i:02d}.png")
            (root / "production.json").write_text(
                json.dumps(doc, ensure_ascii=False), encoding="utf-8",
            )
            production_package.validate_manifest(
                doc, {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()},
            )
            story = ready_production.build_story(doc)
            self.assertEqual(len(story["beats"]), 4)
            self.assertEqual([b["owner_motion"] for b in story["beats"]], ["hold"] * 4)
            self.assertEqual(len({b["owner_asset"] for b in story["beats"]}), 4)
            original_make = owner_art.make_motion_clip
            planned_durations = []

            def render_small(source, dest, **kwargs):
                planned_durations.append(kwargs["seconds"])
                return original_make(
                    source, dest,
                    **{**kwargs, "seconds": 2.15},
                )

            with mock.patch.object(owner_art, "CLIP_WIDTH", 320), \
                 mock.patch.object(owner_art, "CLIP_HEIGHT", 180), \
                 mock.patch.object(owner_art, "MOTION_SUPERSAMPLE", 1), \
                 mock.patch.object(owner_art, "make_motion_clip", side_effect=render_small):
                clips, rights = owner_art.OwnerArtVisualSource(
                    SimpleNamespace(media_transform=None), root,
                ).acquire(
                    {"visual_story": story}, root / "visuals", "podcast", 4,
                    {"s1": 16.0, "s2": 16.0},
                )
            self.assertEqual(len(clips), 4)
            self.assertEqual(len(rights), 4)
            self.assertTrue(all(duration >= 8 for duration in planned_durations))
            self.assertTrue(all(row["grade"] == "podcast_natural_source_ungraded"
                                for row in rights))
            self.assertTrue(all(row["owner_approved"] is True for row in rights))
            visual_samples = []
            for i, clip in enumerate(clips):
                pic = frame_at(clip)
                later = frame_at(clip, 1.4)
                self.assertEqual(pic.size, (320, 180))
                delta = ImageStat.Stat(ImageChops.difference(pic, later)).mean
                self.assertLess(max(delta), 0.5, (i, delta))
                observed = pic.getpixel((160, 90))
                self.assertTrue(all(abs(observed[j] - colours[i][j]) < 18 for j in range(3)),
                                (i, observed, colours[i]))
                visual_samples.append(observed)
                # Each encoded clip is a genuine MP4 with a stationary image.
                probe = subprocess.run(
                    ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=noprint_wrappers=1:nokey=1", str(clip)],
                    capture_output=True, text=True, check=True,
                )
                self.assertGreater(float(probe.stdout.strip()), 2)
            self.assertEqual(len(set(visual_samples)), 4)


if __name__ == "__main__":
    unittest.main()

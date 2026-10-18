"""V24 ZIP camera-move contract: an approved hold is really static.

No provider calls and no external assets. The legacy non-ZIP motion remains intact.
"""
from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
import io
import shutil
import subprocess
from pathlib import Path
from unittest import mock

from PIL import Image

from clean_v2 import owner_art, ready_production
from scripts.test_production_package import archive_bytes, manifest


class ApprovedZipHoldTests(unittest.TestCase):
    @staticmethod
    def _filter(motion_kind: str, respect_hold: bool = False, seconds: float = 17.0) -> str:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch("clean_v2.media._run") as run:
                owner_art.make_motion_clip(
                    root / "source.png",
                    root / "output.mp4",
                    seconds=seconds,
                    direction=1,
                    portrait=True,
                    motion_kind=motion_kind,
                    respect_hold=respect_hold,
                )
            command = run.call_args.args[0]
            return command[command.index("-vf") + 1]

    def test_approved_hold_has_no_camera_move_or_punch(self):
        vf = self._filter("hold", respect_hold=True, seconds=28)
        self.assertIn("zoompan=z='1.0':x='0':y='0'", vf)
        self.assertNotIn("on/", vf)
        self.assertNotIn("0.035", vf)
        self.assertNotIn("0.04", vf)

    def test_legacy_hold_keeps_existing_gentle_motion(self):
        vf = self._filter("hold")
        self.assertIn("zoompan=z='1.0+0.035", vf)
        self.assertIn("on/", vf)

    def test_approved_zoom_and_pan_remain_moving(self):
        for movement in ("slow_pan", "slow_zoom"):
            with self.subTest(movement=movement):
                vf = self._filter(movement, respect_hold=True)
                self.assertIn("zoompan", vf)
                self.assertIn("on/", vf)

    def test_approved_zip_acquisition_preserves_motion_and_transition(self):
        doc = manifest("short")
        doc["scenes"][0]["motion"] = "hold"
        doc["scenes"][1]["motion"] = "slow_zoom"
        doc["scenes"][1]["transition"] = "cut"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "production.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
            with zipfile.ZipFile(io.BytesIO(archive_bytes(doc))) as archive:
                for name in archive.namelist():
                    if name.startswith("assets/"):
                        dest = root / name
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_bytes(archive.read(name))
            calls = []
            def fake_motion(src, dest, **kw):
                calls.append((Path(src).name, dict(kw)))
                Path(dest).write_bytes(b"simulated-video")
                return Path(dest)
            inner = mock.Mock()
            inner.acquire.side_effect = AssertionError("approved assets must not be replaced")
            with mock.patch.object(owner_art, "make_motion_clip", side_effect=fake_motion):
                clips, rights = owner_art.OwnerArtVisualSource(inner, root).acquire(
                    {"visual_story": ready_production.build_story(doc)},
                    root / "render", "short", 7, {"s1": 7.0, "s2": 8.0, "s3": 7.0}
                )
            self.assertEqual(len(clips), len(rights))
            self.assertTrue(all(kwargs["respect_hold"] for _src, kwargs in calls))
            self.assertTrue(any(kwargs["motion_kind"] == "hold" for _src, kwargs in calls))
            self.assertTrue(any(kwargs["motion_kind"] == "slow_zoom" for _src, kwargs in calls))
            self.assertTrue(any(r["owner_transition"] == "cut" for r in rights))
            self.assertTrue(all(r["owner_approved"] for r in rights))
            inner.acquire.assert_not_called()


    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required for encoded-frame proof")
    def test_actual_approved_hold_has_identical_distant_encoded_frames(self):
        """A real 2-second MP4 must have no camera drift, not just a plausible filter."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.png"
            target = root / "static-hold.mp4"
            image = Image.new("RGB", (320, 180))
            pixels = image.load()
            for y in range(180):
                for x in range(320):
                    pixels[x, y] = ((x * 7) % 256, (y * 11) % 256, ((x + y) * 3) % 256)
            image.save(source)
            owner_art.make_motion_clip(
                source, target, seconds=2.0, direction=0,
                portrait=False, motion_kind="hold", respect_hold=True,
            )
            decoded = subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-i", str(target),
                    "-vf", "select=eq(n\\,8)+eq(n\\,45),scale=64:36",
                    "-frames:v", "2", "-f", "rawvideo", "-pix_fmt", "gray", "-",
                ],
                check=True, capture_output=True, timeout=45,
            ).stdout
            frame_size = 64 * 36
            self.assertEqual(len(decoded), 2 * frame_size)
            left, right = decoded[:frame_size], decoded[frame_size:]
            mean_diff = sum(abs(x - y) for x, y in zip(left, right)) / frame_size
            self.assertLessEqual(mean_diff, 0.5, f"approved static hold unexpectedly moved: MAD={mean_diff}")


if __name__ == "__main__":
    unittest.main()

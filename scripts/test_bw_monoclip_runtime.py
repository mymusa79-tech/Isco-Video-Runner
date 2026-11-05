"""Offline runtime guards for BW Monoclip; no external API or TTS calls."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clean_v2 import owner_art
from scripts.test_bw_monoclip_optin import bw_manifest, PEXELS_VIDEO, OTHER_VIDEO


def fixture_video():
    return {
        "provider": "pexels", "media_kind": "video", "asset_id": "28464399",
        "download_url": "https://example.invalid/stock.mp4",
        "source_url": PEXELS_VIDEO, "creator": "fixture",
    }


def write_fixture(_url, path):
    Path(path).write_bytes(b"fixture")


class BwRuntimeTests(unittest.TestCase):
    def test_short_single_video_cannot_continue_as_still(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            owner = owner_art.OwnerArtVisualSource(object(), root)
            beat = {"id": "s2", "section_id": "s2", "owner_stock": {"url": PEXELS_VIDEO}}
            with (
                mock.patch("clean_v2.pinned_stock.fetch_candidate", return_value=fixture_video()),
                mock.patch("clean_v2.media._download_media", side_effect=write_fixture),
                mock.patch("clean_v2.media.probe_duration", return_value=5.0),
                mock.patch.object(owner_art, "clip_is_grayscale") as gray,
            ):
                admitted, reason = owner._try_pinned_one(
                    beat, root / "fallback.png", root, "short", 22.0, 1,
                    PEXELS_VIDEO, bw_cinematic=True,
                )
            self.assertIsNone(admitted)
            self.assertEqual(reason, "pinned_clip_too_short_for_scene")
            gray.assert_not_called()

    def test_monochrome_original_video_allowed_in_bw(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            owner = owner_art.OwnerArtVisualSource(object(), root)
            beat = {"id": "s2", "section_id": "s2", "owner_stock": {"url": PEXELS_VIDEO}}
            with (
                mock.patch("clean_v2.pinned_stock.fetch_candidate", return_value=fixture_video()),
                mock.patch("clean_v2.media._download_media", side_effect=write_fixture),
                mock.patch("clean_v2.media.probe_duration", return_value=33.0),
                mock.patch.object(owner_art, "clip_is_grayscale") as gray,
            ):
                admitted, reason = owner._try_pinned_one(
                    beat, root / "fallback.png", root, "short", 22.0, 1,
                    PEXELS_VIDEO, bw_cinematic=True,
                )
            self.assertEqual(reason, "")
            self.assertIsNotNone(admitted)
            self.assertEqual(admitted[1]["media_kind"], "video")
            gray.assert_not_called()
            self.assertTrue(admitted[0].is_file())

    def test_insufficient_alternatives_never_auto_sequence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            owner = owner_art.OwnerArtVisualSource(object(), root)
            beat = {"id": "s2", "section_id": "s2", "owner_stock": {"urls": [
                PEXELS_VIDEO, OTHER_VIDEO, "https://www.pexels.com/video/walking-1234568/",
            ]}}
            with (
                mock.patch.object(owner, "_try_pinned_one", return_value=(None, "pinned_clip_too_short_for_scene")) as attempt,
                mock.patch.object(owner, "_try_pinned_sequence") as sequence,
            ):
                admitted, reason = owner._try_pinned(
                    beat, root / "fallback.png", root, "short", 25.0, 1,
                    template="micro_story", bw_cinematic=True,
                )
            self.assertIsNone(admitted)
            self.assertIn("pinned_alternatives_exhausted", reason)
            self.assertEqual(attempt.call_count, 3)
            sequence.assert_not_called()

    def test_missing_bw_s2_video_is_fatal(self):
        document = bw_manifest()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "production.json").write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
            beats = []
            for scene in document["scenes"]:
                asset = root / scene["asset"]
                asset.parent.mkdir(parents=True, exist_ok=True)
                asset.write_bytes(b"fixture")
                beat = {
                    "id": scene["id"], "section_id": scene["section_id"],
                    "owner_asset": scene["asset"], "owner_motion": scene["motion"],
                    "owner_transition": scene["transition"],
                }
                if "stock" in scene:
                    beat["owner_stock"] = scene["stock"]
                beats.append(beat)
            owner = owner_art.OwnerArtVisualSource(object(), root)
            with (
                mock.patch.object(owner_art, "make_motion_clip", return_value=root / "owner-still.mp4"),
                mock.patch.object(owner, "_try_pinned", return_value=(None, "pinned_unavailable")),
            ):
                with self.assertRaisesRegex(RuntimeError, "bw_cinematic_main_clip_unavailable"):
                    owner._acquire_ready(
                        {"visual_story": {"beats": beats}}, root / "out", "short",
                        {"s1": 6.0, "s2": 22.0, "s3": 5.0},
                    )


if __name__ == "__main__":
    unittest.main()

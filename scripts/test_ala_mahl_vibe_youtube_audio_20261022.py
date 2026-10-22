"""Separate optional Ala Mahl Vibe contract; never change legacy Story ZIPs."""
from __future__ import annotations

import copy
import io
import json
import unittest
import zipfile

from clean_v2 import production_package as pkg
from clean_v2.owner_pack import OwnerPackError
from clean_v2.ready_production import build_story
from scripts.test_production_package import archive_bytes, manifest


def vibe_doc():
    doc = manifest(template="ala_mahl")
    doc["ala_mahl_mode"] = "vibe"
    doc["editing"] = {
        "music": "youtube_audio_library",
        "mood": "nostalgic",
        "asset": "assets/vibe_music.mp3",
        "track_title": "Editor confirmed in Studio",
        "artist": "Verified credited composer",
        "license_type": "youtube_audio_library_standard",
    }
    extra = doc["script"]["sections"][0]["text"].split(". ", 1)[1]
    doc["scenes"].insert(1, {
        "id": "image_4", "section_id": "s1",
        "asset": "assets/scene_04.png",
        "start_text": extra, "motion": "hold", "transition": "cut",
    })
    for i, item in enumerate(doc["scenes"]):
        item["stock"] = {"url": f"https://www.pexels.com/video/real-moving-footage-{123456+i}/"}
    return doc


def demo_mp3_header():
    # Import checks MP3 type/size only. Runtime ffprobe independently verifies
    # decodability BEFORE voice costs; fake ID3 is never a production music asset.
    return b"ID3" + bytes(range(256)) * 12


class VibeContractTests(unittest.TestCase):
    def test_legacy_story_preserved(self):
        doc = manifest(template="ala_mahl")
        self.assertEqual(pkg.validate_manifest(doc), doc)
        self.assertEqual(doc["editing"], {"music": "none"})
        self.assertNotIn("ala_mahl_mode", doc)
        with self.assertRaises(OwnerPackError):
            legacy = copy.deepcopy(doc)
            legacy["editing"]["music"] = "youtube_audio_library"
            pkg.validate_manifest(legacy)

    def test_separate_four_pinned_motion_scenes_and_human_selected_music(self):
        doc = vibe_doc()
        expected = {scene["asset"] for scene in doc["scenes"]}
        expected.add("assets/vibe_music.mp3")
        self.assertEqual(pkg.validate_manifest(doc, expected), doc)
        story = build_story(doc)
        self.assertEqual(len(story["beats"]), 4)
        self.assertTrue(all(b.get("owner_vibe") for b in story["beats"]))
        self.assertEqual(len([b for b in story["beats"] if "owner_stock" in b]), 4)
        self.assertFalse(any("owner_board" in b for b in story["beats"]))
        z = archive_bytes(doc, extra={"assets/vibe_music.mp3": demo_mp3_header()})
        parsed = pkg.inspect_archive(z)
        self.assertEqual(parsed["manifest"]["editing"]["track_title"], "Editor confirmed in Studio")
        self.assertIn("assets/vibe_music.mp3", parsed["asset_sha256"])

    def test_missing_music_rejected_at_import(self):
        with self.assertRaisesRegex(OwnerPackError, "مفقودة"):
            pkg.inspect_archive(archive_bytes(vibe_doc()))

    def test_unsupported_audio_license_and_old_freepd_rejected(self):
        for invalid in (
            {"license_type": "unknown"},
            {"music": "vibe_instrumental"},
            {"asset": "../music.mp3"},
            {"mood": "angry"},
            {"track_title": ""},
        ):
            doc = vibe_doc()
            doc["editing"].update(invalid)
            with self.subTest(invalid=invalid), self.assertRaises(OwnerPackError):
                pkg.validate_manifest(doc)

    def test_non_vibe_cannot_attach_audio_or_activate_vibe(self):
        doc = manifest(template="ala_mahl")
        with self.assertRaises(OwnerPackError):
            pkg.inspect_archive(archive_bytes(doc, extra={"assets/vibe_music.mp3": demo_mp3_header()}))
        doc["ala_mahl_mode"] = "vibe"
        with self.assertRaises(OwnerPackError):
            pkg.validate_manifest(doc)

    def test_vibe_rejects_wrong_footage_or_missing_motion(self):
        for stock in (
            {"url": "https://www.pexels.com/photo/photo-123456/"},
            {"url": "https://pixabay.com/photos/old-lamp-123456/"},
            {"url": "https://coverr.co/videos/old-clip-abcdefgh"},
            {"query_en": "real moving person"},
        ):
            doc = vibe_doc()
            doc["scenes"][0]["stock"] = stock
            with self.subTest(stock=stock), self.assertRaises(OwnerPackError):
                pkg.validate_manifest(doc)
        doc = vibe_doc()
        doc["scenes"][1].pop("stock")
        with self.assertRaises(OwnerPackError):
            pkg.validate_manifest(doc)

    def test_vibe_rejects_extra_audio_or_invalid_header(self):
        doc = vibe_doc()
        with self.assertRaises(OwnerPackError):
            pkg.inspect_archive(archive_bytes(doc, extra={"assets/vibe_music.mp3": b"not-mp3" * 600}))
        with self.assertRaises(OwnerPackError):
            pkg.inspect_archive(archive_bytes(doc, extra={
                "assets/vibe_music.mp3": demo_mp3_header(),
                "assets/unapproved.mp3": demo_mp3_header(),
            }))

    def test_vibe_does_not_override_short_templates(self):
        doc = manifest()
        doc["ala_mahl_mode"] = "vibe"
        with self.assertRaises(OwnerPackError):
            pkg.validate_manifest(doc)


if __name__ == "__main__":
    unittest.main()

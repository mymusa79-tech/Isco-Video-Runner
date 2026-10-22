"""Opt-in Vibe media contract: no changes to existing Ala Mahl Story."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clean_v2 import owner_art, production_package as pkg, short_audio_polish
from clean_v2.owner_pack import OwnerPackError
from clean_v2.ready_production import build_story
from scripts.test_production_package import archive_bytes, manifest


def vibe_manifest(count: int = 4) -> dict:
    doc = manifest("short", template="ala_mahl")
    anchors = [
        ("s1", "لم يكن يحتاج إلى يوم مثالي كي يبدأ."),
        ("s1", "جلس أمام فنجانه البارد،"),
        ("s1", "ثم فتح النافذة"),
        ("s2", "في البداية بدت الخطوة أصغر من أن تصنع فرقًا."),
        ("s2", "لكنه أكملها بهدوء،"),
        ("s3", "حين عاد إلى فنجانه كان يومه قد تغيّر قليلًا."),
        ("s3", "ابتسم للباب المفتوح،"),
    ]
    selected = [anchors[i] for i in ({4: (0, 1, 3, 5), 7: tuple(range(7))}[count])]
    doc["ala_mahl_mode"] = "vibe"
    doc["editing"] = {
        "music": "youtube_audio_library",
        "mood": "hopeful",
        "asset": "assets/vibe_music.mp3",
        "track_title": "Editor selected",
        "artist": "Editor verified artist",
        "license_type": "youtube_audio_library_standard",
    }
    doc["scenes"] = [
        {
            "id": f"v{i}", "section_id": section_id,
            "asset": f"assets/vibe_{i:02d}.png",
            "start_text": anchor,
            "motion": "hold", "transition": "cut",
            "stock": {"url": f"https://www.pexels.com/video/{100000 + i}/"},
        }
        for i, (section_id, anchor) in enumerate(selected, 1)
    ]
    return doc


class VibeArchiveContractTests(unittest.TestCase):
    def test_vibe_four_and_seven_valid_scene_contracts(self):
        for count in (4, 7):
            with self.subTest(count=count):
                doc = vibe_manifest(count)
                pkg.validate_manifest(doc)
                inspected = pkg.inspect_archive(archive_bytes(doc, extra={"assets/vibe_music.mp3": b"ID3" + bytes(range(256)) * 12}))
                self.assertEqual(inspected["manifest"]["ala_mahl_mode"], "vibe")
                beats = build_story(doc)["beats"]
                self.assertEqual(len(beats), count)  # no virtual still after hook
                self.assertTrue(all(beat["owner_vibe"] for beat in beats))
                self.assertTrue(all(beat["owner_stock"]["url"] for beat in beats))

    def test_story_legacy_fully_unchanged(self):
        old = manifest("short", template="ala_mahl")
        before = copy.deepcopy(old)
        self.assertIs(pkg.validate_manifest(old), old)
        self.assertEqual(old, before)
        self.assertEqual(pkg.validate_manifest(old)["editing"], {"music": "none"})

    def test_reject_vibe_outside_ala_mahl(self):
        doc = vibe_manifest()
        doc["template"] = "micro_story"
        with self.assertRaises(OwnerPackError):
            pkg.validate_manifest(doc)

    def test_reject_three_scenes_and_wrong_track(self):
        doc = vibe_manifest()
        doc["scenes"] = doc["scenes"][:3]
        with self.assertRaises(OwnerPackError):
            pkg.validate_manifest(doc)
        doc = vibe_manifest()
        doc["editing"]["license_type"] = "random-web-song"
        with self.assertRaises(OwnerPackError):
            pkg.validate_manifest(doc)

    def test_reject_non_string_editor_music_choices(self):
        for field in ("mood", "track_title", "artist"):
            with self.subTest(field=field):
                doc = vibe_manifest()
                doc["editing"][field] = ["not", "a", "string"]
                with self.assertRaises(OwnerPackError):
                    pkg.validate_manifest(doc)

    def test_reject_search_photo_coverr_and_unpinned_vibe_stock(self):
        for stock in (
            {"query_en": "man walking in street"},
            {"url": "https://www.pexels.com/photo/123456/"},
            {"url": "https://pixabay.com/photos/garden-123456/"},
            {"url": "https://coverr.co/videos/some-example-12345678"},
        ):
            with self.subTest(stock=stock):
                doc = vibe_manifest()
                doc["scenes"][0]["stock"] = stock
                with self.assertRaises(OwnerPackError):
                    pkg.validate_manifest(doc)

    def test_existing_story_still_cannot_request_music(self):
        doc = manifest("short", template="ala_mahl")
        doc["editing"] = {"music": "vibe_instrumental"}
        with self.assertRaises(OwnerPackError):
            pkg.validate_manifest(doc)

    def test_vibe_requires_explicit_scene_for_every_section(self):
        doc = vibe_manifest()
        for scene in doc["scenes"]:
            if scene["section_id"] == "s3":
                scene["section_id"] = "s2"
        with self.assertRaises(OwnerPackError):
            pkg.validate_manifest(doc)

    def test_vibe_rejects_board(self):
        doc = vibe_manifest()
        doc["scenes"][1]["board"] = {"layout": "steps", "items": []}
        with self.assertRaises(OwnerPackError):
            pkg.validate_manifest(doc)


class VibeRendererTests(unittest.TestCase):
    def test_motion_source_rejects_photo_before_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = owner_art.OwnerArtVisualSource(mock.Mock(), root)
            with mock.patch("clean_v2.pinned_stock.fetch_candidate", return_value={
                "provider": "pexels", "media_kind": "photo",
            }), mock.patch("clean_v2.media._download_media", side_effect=AssertionError("no photo download")):
                result, reason = source._try_pinned(
                    {
                        "id": "v1", "owner_vibe": True,
                        "owner_stock": {"url": "https://www.pexels.com/photo/123456/"},
                    },
                    root / "fallback.png", root, "short", 12.0, 0,
                )
            self.assertIsNone(result)
            self.assertEqual(reason, "vibe_requires_real_motion_stock")

    def test_youtube_audio_library_asset_does_not_use_freepd(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "production.json").write_text(json.dumps(vibe_manifest()), encoding="utf-8")
            final = root / "final.mp4"
            final.write_bytes(b"original export")
            assets = root / "art" / "assets"
            assets.mkdir(parents=True)
            (assets / "vibe_music.mp3").write_bytes(b"fixture mocked decoder")
            def mock_mix(*, output_path, **_):
                output_path.write_bytes(b"new final")
            probe = json.dumps({"streams": [{"codec_type": "audio", "codec_name": "mp3"}],
                                "format": {"duration": "73"}})
            with (
                mock.patch("clean_v2.owner_art.art_dir", return_value=root / "art"),
                mock.patch("clean_v2.music_library.ensure_music_library",
                           side_effect=AssertionError("FreePD must not be used")),
                mock.patch("clean_v2.short_audio_polish.select_music_track",
                           side_effect=AssertionError("Coverr and stock music search prohibited")),
                mock.patch("clean_v2.short_audio_polish._measure_mean_db", return_value=-20.0),
                mock.patch("clean_v2.short_audio_polish._topic_window", return_value=(4.0, 30.0)),
                mock.patch("clean_v2.short_audio_polish._run", return_value=mock.Mock(stdout=probe)),
                mock.patch("clean_v2.short_audio_polish._prepare_local_music_bed"),
                mock.patch("clean_v2.short_audio_polish._normalize_relative",
                           return_value={"relative_to_narration_db": -27}),
                mock.patch("clean_v2.short_audio_polish._editorial_audio_windows", return_value=[]),
                mock.patch("clean_v2.short_audio_polish._mix_music_into_video", side_effect=mock_mix),
            ):
                result = short_audio_polish.apply_topic_audio_polish(
                    output_dir=root, final_path=final,
                    narration_path=root / "narration.wav", fmt="short",
                )
            self.assertEqual(result["status"], "pass")
            self.assertEqual(result["asset_origin"], "user_downloaded_youtube_studio_audio_library")
            self.assertEqual(final.read_bytes(), b"new final")

    def test_missing_studio_music_preserves_final_and_fails_vibe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "production.json").write_text(json.dumps(vibe_manifest()), encoding="utf-8")
            final = root / "final.mp4"
            final.write_bytes(b"original export")
            with (
                mock.patch("clean_v2.owner_art.art_dir", return_value=root),
                mock.patch("clean_v2.short_audio_polish._measure_mean_db", return_value=-20.0),
                mock.patch("clean_v2.short_audio_polish._topic_window", return_value=(4.0, 30.0)),
            ):
                with self.assertRaisesRegex(RuntimeError, "vibe_approved_music_mixing_failed"):
                    short_audio_polish.apply_topic_audio_polish(
                        output_dir=root, final_path=final,
                        narration_path=root / "narration.wav", fmt="short",
                    )
            self.assertEqual(final.read_bytes(), b"original")


if __name__ == "__main__":
    unittest.main()

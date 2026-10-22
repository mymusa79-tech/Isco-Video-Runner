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
        "music": "vibe_instrumental",
        "mood": "hopeful",
        "track_id": "wonder-flow",
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
                inspected = pkg.inspect_archive(archive_bytes(doc))
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
        doc["editing"]["track_id"] = "random-web-song"
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

    def test_editor_selected_cc0_track_does_not_call_stock_music_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "production.json").write_text(json.dumps(vibe_manifest()), encoding="utf-8")
            final = root / "final.mp4"
            final.write_bytes(b"original export")
            track = root / "approved.mp3"
            track.write_bytes(b"verified track mock")
            def write_mix(*, output_path, **_):
                output_path.write_bytes(b"mixed export")
            with (
                mock.patch("clean_v2.music_library.ensure_music_library", return_value={
                    "ready": [{"id": "wonder-flow", "path": str(track), "title": "Wonder Flow"}],
                    "unavailable": [], "source": "verified FreePD", "license": "CC0",
                }) as catalog,
                mock.patch("clean_v2.short_audio_polish.select_music_track", side_effect=AssertionError("no Coverr search")),
                mock.patch("clean_v2.short_audio_polish._measure_mean_db", return_value=-20.0),
                mock.patch("clean_v2.short_audio_polish._topic_window", return_value=(4.0, 30.0)),
                mock.patch("clean_v2.short_audio_polish._prepare_local_music_bed"),
                mock.patch("clean_v2.short_audio_polish._normalize_relative", return_value={"relative_to_narration_db": -27}),
                mock.patch("clean_v2.short_audio_polish._editorial_audio_windows", return_value=[]),
                mock.patch("clean_v2.short_audio_polish._mix_music_into_video", side_effect=write_mix),
            ):
                result = short_audio_polish.apply_topic_audio_polish(
                    output_dir=root, final_path=final, narration_path=root / "narration.wav", fmt="short",
                )
            self.assertEqual(result["status"], "pass")
            self.assertEqual(result["components"]["music"]["track_id"], "wonder-flow")
            self.assertEqual(final.read_bytes(), b"mixed export")
            catalog.assert_called_once_with(allow_download=True, track_ids=["wonder-flow"])

    def test_if_track_missing_final_video_is_not_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "production.json").write_text(json.dumps(vibe_manifest()), encoding="utf-8")
            final = root / "final.mp4"
            final.write_bytes(b"original")
            with (
                mock.patch("clean_v2.music_library.ensure_music_library", return_value={"ready": [], "unavailable": []}),
                mock.patch("clean_v2.short_audio_polish._measure_mean_db", return_value=-20.0),
                mock.patch("clean_v2.short_audio_polish._topic_window", return_value=(4.0, 30.0)),
            ):
                result = short_audio_polish.apply_topic_audio_polish(
                    output_dir=root, final_path=final, narration_path=root / "narration.wav", fmt="short",
                )
            self.assertEqual(result["status"], "skipped_fail_safe")
            self.assertEqual(final.read_bytes(), b"original")


if __name__ == "__main__":
    unittest.main()

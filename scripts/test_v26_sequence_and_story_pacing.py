"""V26.2 opt-in stock montage, old ZIP compatibility and editorial still-pacing reports."""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from clean_v2 import owner_art, production_package
from clean_v2.media import probe_duration
from clean_v2.simple_mode import _story_pacing_audit
from scripts.test_production_package import manifest

A = "https://www.pexels.com/video/office-workers-3195394/"
B = "https://pixabay.com/videos/office-desk-work-99999/"
C = "https://www.pexels.com/video/morning-work-9999999/"
PHOTO = "https://www.pexels.com/photo/empty-room-1234567/"
COVERR = "https://coverr.co/videos/aerial-view-of-a-street-jixathxhhs"


class SequenceContractTests(unittest.TestCase):
    def test_sequence_for_short_and_film_only(self):
        for fmt in ("short", "film"):
            doc = manifest(fmt)
            doc["scenes"][0]["stock"] = {"urls": [A, B, C], "mode": "sequence",
                                         "must_show": "hands finishing work"}
            self.assertEqual(production_package.validate_manifest(doc)["scenes"][0]["stock"]["mode"], "sequence")
        doc = manifest("podcast")
        doc["scenes"][0]["stock"] = {"urls": [A, B, C], "mode": "sequence"}
        with self.assertRaises(Exception):
            production_package.validate_manifest(doc)
        doc = manifest("short", template="ala_mahl")
        doc["scenes"][0]["stock"] = {"urls": [A, B, C], "mode": "sequence"}
        with self.assertRaises(Exception):
            production_package.validate_manifest(doc)

    def test_legacy_three_urls_remain_alternatives(self):
        doc = manifest("short")
        doc["scenes"][0]["stock"] = {"urls": [A, B, C]}
        result = production_package.validate_manifest(doc)
        self.assertNotIn("mode", result["scenes"][0]["stock"])

    def test_photo_coverr_and_wrong_count_are_rejected_in_sequence(self):
        for links in ([A, B, PHOTO], [A, B, COVERR], [A, B]):
            with self.subTest(links=links):
                doc = manifest("film")
                doc["scenes"][0]["stock"] = {"urls": links, "mode": "sequence"}
                with self.assertRaises(Exception):
                    production_package.validate_manifest(doc)


class SequenceAssemblyTests(unittest.TestCase):
    def test_order_and_rejected_alternate_without_real_network(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = owner_art.OwnerArtVisualSource(types.SimpleNamespace(), root)
            first = root / "a.mp4"; first.write_bytes(b"video one")
            third = root / "c.mp4"; third.write_bytes(b"video three")
            rows = [
                ((first, {"provider": "pexels", "asset_id": "1", "source_url": A,
                           "media_kind": "video", "source_actual": "stock_motion"}), ""),
                (None, "unavailable"),
                ((third, {"provider": "pixabay", "asset_id": "3", "source_url": C,
                           "media_kind": "video", "source_actual": "stock_motion"}), ""),
            ]
            beat = {"id": "scene1", "owner_stock": {"urls": [A, B, C], "mode": "sequence"},
                    "owner_motion": "hold"}
            def fake_run(args, timeout=None):
                (root / "visual-stock-scene1.mp4").write_bytes(b"joined")
            with mock.patch.object(source, "_try_pinned_one", side_effect=rows) as attempts, \
                 mock.patch("clean_v2.media.probe_duration", side_effect=[2.0, 2.0]), \
                 mock.patch.object(owner_art, "make_motion_clip") as tail, \
                 mock.patch("clean_v2.media._run", side_effect=fake_run):
                admitted, reason = source._try_pinned(beat, root / "owner.jpg", root, "film", 5.0, 0)
            self.assertEqual(reason, "")
            self.assertEqual(attempts.call_count, 3)
            self.assertEqual(tail.call_count, 1)
            self.assertEqual(admitted[1]["pinned_sequence_used"], 2)
            self.assertEqual(admitted[1]["pinned_source_seconds"], 4.0)
            self.assertEqual([s["source_url"] for s in admitted[1]["pinned_sequence_sources"]], [A, C])
            self.assertTrue(admitted[1]["pinned_continues_with_image"])
            self.assertTrue(admitted[1]["sequence_visual_review_required"])

    def test_all_failed_falls_back_to_existing_image(self):
        source = owner_art.OwnerArtVisualSource(types.SimpleNamespace(), Path("."))
        beat = {"id": "scene1", "owner_stock": {"urls": [A, B, C], "mode": "sequence"}}
        with mock.patch.object(source, "_try_pinned_one", return_value=(None, "rejected")):
            chosen, reason = source._try_pinned(beat, Path("owner.jpg"), Path("."), "short", 7.0, 0)
        self.assertIsNone(chosen)
        self.assertIn("pinned_sequence_exhausted:", reason)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg needed")
class RealMontageTests(unittest.TestCase):
    def test_ffmpeg_joins_short_clips_without_audio_or_repeat(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = []
            for ix, color in enumerate(("red", "green", "blue")):
                path = root / f"{ix}.mp4"
                subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                                "-f", "lavfi", "-i", f"color=c={color}:s=160x90:r=15:d=1.7",
                                "-an", "-pix_fmt", "yuv420p", str(path)], check=True, timeout=60)
                paths.append(path)
            source = owner_art.OwnerArtVisualSource(types.SimpleNamespace(), root)
            row = lambda i: {"provider": "pexels", "asset_id": str(i),
                             "source_url": [A, B, C][i], "media_kind": "video",
                             "source_actual": "stock_motion"}
            beat = {"id": "test", "owner_stock": {"urls": [A, B, C], "mode": "sequence"},
                    "owner_motion": "hold"}
            with mock.patch.object(source, "_try_pinned_one", side_effect=[((paths[i], row(i)), "") for i in range(3)]):
                admitted, reason = source._try_pinned(beat, root / "owner.jpg", root, "film", 4.0, 0)
            self.assertEqual(reason, "")
            self.assertEqual(admitted[1]["pinned_sequence_used"], 3)
            self.assertAlmostEqual(probe_duration(admitted[0]), 4.0, delta=0.25)
            probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a",
                                    "-show_entries", "stream=codec_type",
                                    "-of", "csv=p=0", str(admitted[0])],
                                   capture_output=True, text=True, check=True)
            self.assertEqual(probe.stdout.strip(), "")


class PacingAuditTests(unittest.TestCase):
    def write(self, root, fmt, template, secs, assets):
        doc = {"format": fmt, "template": template,
               "scenes": [{"asset": name} for name in assets]}
        (root / "production.json").write_text(json.dumps(doc), encoding="utf-8")
        (root / "timeline-first.json").write_text(json.dumps({
            "voice_seconds_measured": secs, "status": "pass"}), encoding="utf-8")
        (root / "rights-manifest.json").write_text(json.dumps({"assets": [
            {"source_actual": "owner_art", "beat_id": f"s{i}", "local_file": f"s{i}.mp4"}
            for i in range(len(assets))]}), encoding="utf-8")

    def test_latest_podcast_with_one_image_raises_advisory_not_block(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp);self.write(root, "podcast", "dialogue_qa", 898.4, ["rope.jpg"])
            result = _story_pacing_audit(root, "podcast")
            self.assertIn("podcast_single_photo_for_long_dialogue", result["warnings"])
            self.assertEqual(result["status"], "review_required")

    def test_ala_mahl_over_55_seconds_is_flagged(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp);self.write(root, "short", "ala_mahl", 75.7, ["a.jpg", "b.jpg"])
            result = _story_pacing_audit(root, "short")
            self.assertIn("ala_mahl_longer_than_55_seconds", result["warnings"])
            self.assertIn("ala_mahl_not_enough_distinct_story_moments", result["warnings"])

    def test_film_does_not_inherit_ala_mahl_warnings(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp);self.write(root, "film", "direct_cinematic", 500, ["a.jpg"])
            self.assertEqual(_story_pacing_audit(root, "film"), {})


if __name__ == "__main__":
    unittest.main()

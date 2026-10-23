"""Regression for private, hash-pinned owner YouTube Audio Library music beds."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clean_v2 import owner_youtube_music as owner_music
from clean_v2 import production_package
from clean_v2.short_audio_polish import apply_topic_audio_polish
from scripts.test_production_package import archive_bytes, manifest


def with_music(fmt: str = "short", template=None, track: str = "lift-up") -> dict:
    doc = manifest(fmt, template=template)
    doc["editing"] = {"music": "youtube_audio_library", "track_id": track,
                      "asset": owner_music.ASSET, "delay_seconds": 3.0}
    return doc


class MusicContractTests(unittest.TestCase):
    def test_none_remains_default_and_additional_fields_are_rejected(self):
        self.assertEqual(production_package.validate_manifest(manifest())["editing"], {"music": "none"})
        doc = manifest(); doc["editing"]["track_id"] = "lift-up"
        with self.assertRaises(Exception):
            production_package.validate_manifest(doc)

    def test_music_requires_correct_asset_and_mood_format(self):
        doc = with_music()
        self.assertEqual(production_package.validate_manifest(doc)["editing"]["track_id"], "lift-up")
        doc["editing"]["track_id"] = "after-sunset"
        with self.assertRaises(Exception):
            production_package.validate_manifest(doc)
        doc = with_music("podcast", track="after-sunset")
        self.assertEqual(production_package.validate_manifest(doc)["editing"]["track_id"], "after-sunset")
        doc = with_music("short", "ala_mahl", "forgiven-fate")
        self.assertEqual(production_package.validate_manifest(doc)["editing"]["track_id"], "forgiven-fate")
        doc["editing"]["track_id"] = "lift-up"
        with self.assertRaises(Exception):
            production_package.validate_manifest(doc)

    def test_delay_guard_and_audio_not_listed_as_image(self):
        doc = with_music()
        for bad in (0, -1, 13, True, "3"):
            with self.subTest(delay=bad):
                doc["editing"]["delay_seconds"] = bad
                with self.assertRaises(Exception):
                    production_package.validate_manifest(doc)

    def test_approved_zip_accepts_only_exact_hash_pinned_file(self):
        src = b"ID3" + bytes(range(256)) * 20
        with mock.patch.dict(owner_music.TRACKS, {"test-track": {
            "title": "test", "artist": "test",
            "sha256": hashlib.sha256(src).hexdigest(),
            "formats": ("short",), "mood": "test"
        }}):
            doc = with_music(track="test-track")
            data = archive_bytes(doc, extra={owner_music.ASSET: src})
            inspected = production_package.inspect_archive(data)
            self.assertIn(owner_music.ASSET, inspected["asset_sha256"])
            with self.assertRaises(Exception):
                production_package.inspect_archive(archive_bytes(doc, extra={owner_music.ASSET: b"ID3" + b"X" * 2100}))
            with self.assertRaises(Exception):
                production_package.inspect_archive(archive_bytes(doc))
            with self.assertRaises(Exception):
                production_package.inspect_archive(archive_bytes(manifest(), extra={owner_music.ASSET: src}))
            with self.assertRaises(Exception):
                production_package.inspect_archive(archive_bytes(doc, extra={"assets/untrusted.mp3": src}))

    def test_unknown_track_never_admitted(self):
        doc = with_music(track="unknown-track")
        with self.assertRaises(Exception):
            production_package.validate_manifest(doc)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "requires ffmpeg")
class MusicActualRenderTests(unittest.TestCase):
    def test_quiet_music_joins_only_after_topic_start_while_video_length_stays_constant(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            out = root / "output"; out.mkdir()
            art = root / "private_zip" / "assets"; art.mkdir(parents=True)
            music = art / "music_bed.mp3"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                            "-i", "sine=frequency=780:duration=9:sample_rate=44100",
                            "-c:a", "libmp3lame", "-b:a", "128k", str(music)], check=True, timeout=50)
            original_hash = owner_music.TRACKS["lift-up"]["sha256"]
            with mock.patch.dict(owner_music.TRACKS, {"fixture-music": {
                "title": "Fixture", "artist": "offline",
                "sha256": hashlib.sha256(music.read_bytes()).hexdigest(),
                "formats": ("short",), "mood": "test"
            }}), mock.patch.dict(os.environ, {"CLEAN_V2_OWNER_ART_DIR": str(art.parent)}):
                (out / "production.json").write_text(json.dumps({
                    "format": "short", "template": "micro_story",
                    "editing": {"music": "youtube_audio_library", "track_id": "fixture-music",
                                "asset": owner_music.ASSET, "delay_seconds": 2}
                }), encoding="utf-8")
                (out / "timeline-first.json").write_text(json.dumps({
                    "identity_events": [{"kind": "topic", "start": 2, "end": 10}]
                }), encoding="utf-8")
                voice = root / "voice.wav"; video = out / "final.mp4"
                subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                                "-f", "lavfi", "-i", "sine=frequency=440:duration=12:sample_rate=44100",
                                "-c:a", "pcm_s16le", str(voice)], check=True, timeout=50)
                subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                                "-f", "lavfi", "-i", "color=c=gray:s=160x90:r=15:d=12",
                                "-i", str(voice), "-c:v", "libx264", "-preset", "ultrafast",
                                "-c:a", "aac", "-shortest", str(video)], check=True, timeout=90)
                result = apply_topic_audio_polish(
                    output_dir=out, final_path=video, narration_path=voice,
                    script={}, fmt="short"
                )
                self.assertEqual(result["status"], "pass", result.get("reason"))
                self.assertTrue(result["music_added"])
                self.assertFalse(result["voice_changed"])
                self.assertFalse(result["external_download_required"])
                self.assertEqual(result["music_window"]["start"], 4.0)
                probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                                        "format=duration", "-of", "json", str(video)],
                                       capture_output=True, text=True, check=True)
                self.assertAlmostEqual(float(json.loads(probe.stdout)["format"]["duration"]), 12, delta=.35)


if __name__ == "__main__":
    unittest.main()

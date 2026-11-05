"""Prayer-free Cinematic Monoclip contract tests (offline; no TTS or media upload)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clean_v2 import identity_sequence, owner_pack, timeline_first, timeline_render
from scripts.test_production_package import imported_pack, manifest


def samples():
    return [
        {"role": "hook", "start": 0.0, "end": 5.0},
        {"role": "post_hook_silence", "start": 5.0, "end": 5.25},
        {"role": "topic", "start": 5.25, "end": 23.7},
    ]


class MonoclipIdentityTests(unittest.TestCase):
    def test_approved_both_color_and_bw_have_no_prayer_from_import(self):
        for style in ("color_cinematic", "bw_cinematic"):
            with self.subTest(style=style), tempfile.TemporaryDirectory() as temp:
                doc = manifest()
                doc["visual_style"] = style
                doc["scenes"][1]["stock"] = {
                    "url": "https://www.pexels.com/video/a-man-walking-down-a-path-in-the-woods-28464399/"
                }
                script = owner_pack.build_artifacts(
                    imported_pack(doc), {"format": "short"}, Path(temp)
                )
                spoken = " ".join(s["narration"] for s in script["sections"])
                self.assertNotIn(identity_sequence.PRAYER_SENTENCE, spoken)
                self.assertEqual(
                    json.loads((Path(temp) / "narrative-identity.json").read_text())["prayer_sentence"], ""
                )
                self.assertEqual(
                    json.loads((Path(temp) / "production.json").read_text())["visual_style"], style
                )

    def test_existing_short_retains_original_prayer(self):
        with tempfile.TemporaryDirectory() as temp:
            script = owner_pack.build_artifacts(
                imported_pack(manifest()), {"format": "short"}, Path(temp)
            )
            spoken = " ".join(s["narration"] for s in script["sections"])
            self.assertEqual(spoken.count(identity_sequence.PRAYER_SENTENCE), 1)
            self.assertEqual(
                json.loads((Path(temp) / "narrative-identity.json").read_text())["prayer_sentence"],
                identity_sequence.PRAYER_SENTENCE,
            )

    def test_monoclip_timeline_ends_with_spoken_story_no_outro(self):
        events = timeline_first._identity_events(
            samples(), voice_seconds=23.7, require_identity=True,
            fmt="short", template="micro_story", cinematic_monoclip=True,
        )
        kinds = [item["kind"] for item in events]
        self.assertIn("hook", kinds)
        self.assertIn("topic", kinds)
        self.assertNotIn("outro", kinds)
        self.assertNotIn("final_silence", kinds)
        self.assertNotIn("prayer", kinds)
        self.assertNotIn("post_prayer_silence", kinds)
        topic = next(e for e in events if e["kind"] == "topic")
        self.assertEqual(topic["start"], 5.25)
        self.assertEqual(topic["end"], 23.7)

    def test_monoclip_identity_compositor_copies_without_assets_or_sfx(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            source = folder / "body.mp4"
            output = folder / "rendered.mp4"
            source.write_bytes(b"video-and-voice-without-outro")
            timeline = {
                "template": "micro_story", "voice_seconds_measured": 23.7,
                "cinematic_monoclip": True,
                "identity_events": [
                    {"kind": "hook", "start": 0, "end": 5},
                    {"kind": "post_hook_silence", "start": 5, "end": 5.25},
                    {"kind": "topic", "start": 5.25, "end": 23.7},
                ],
            }
            with (
                mock.patch.object(timeline_render, "identity_asset_paths") as assets,
                mock.patch.object(timeline_render.subprocess, "run") as command,
            ):
                timeline_render._render_short_identity(source, output, timeline=timeline)
            self.assertEqual(output.read_bytes(), source.read_bytes())
            assets.assert_not_called()
            command.assert_not_called()

    def test_legacy_short_cannot_bypass_identity_when_events_missing(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / "body.mp4").write_bytes(b"test")
            with self.assertRaisesRegex(RuntimeError, "short_identity_missing_prayer_or_outro"):
                timeline_render._render_short_identity(
                    folder / "body.mp4", folder / "rendered.mp4",
                    timeline={"template": "micro_story", "identity_events": []},
                )
            self.assertFalse((folder / "rendered.mp4").exists())

    def test_normal_short_outro_timeline_unchanged(self):
        with_prayer = [
            {"role": "hook", "start": 0.0, "end": 3.0},
            {"role": "post_hook_silence", "start": 3.0, "end": 3.25},
            {"role": "prayer", "start": 3.25, "end": 6.0},
            {"role": "post_prayer_silence", "start": 6.0, "end": 6.25},
            {"role": "topic", "start": 6.25, "end": 19.3},
            {"role": "final_silence", "start": 19.3, "end": 22.0},
        ]
        events = timeline_first._identity_events(
            with_prayer, voice_seconds=22.0, require_identity=True,
            fmt="short", template="micro_story", cinematic_monoclip=False,
        )
        kinds = [event["kind"] for event in events]
        self.assertIn("prayer", kinds)
        self.assertIn("outro", kinds)
        self.assertIn("final_silence", kinds)


if __name__ == "__main__":
    unittest.main()

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
        {"role": "topic", "start": 5.25, "end": 21.0},
        {"role": "final_silence", "start": 21.0, "end": 23.7},
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

    def test_monoclip_timeline_has_hook_topic_and_end_card_only(self):
        events = timeline_first._identity_events(
            samples(), voice_seconds=23.7, require_identity=True,
            fmt="short", template="micro_story", cinematic_monoclip=True,
        )
        kinds = [item["kind"] for item in events]
        self.assertIn("hook", kinds)
        self.assertIn("topic", kinds)
        self.assertIn("outro", kinds)
        self.assertNotIn("prayer", kinds)
        self.assertNotIn("post_prayer_silence", kinds)
        topic = next(e for e in events if e["kind"] == "topic")
        self.assertEqual(topic["start"], 5.25)
        self.assertEqual(topic["end"], 21.0)

    def test_monoclip_visual_composition_never_opens_prayer_asset(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            outro = folder / "subscribe-outro.mp4"
            outro.write_bytes(b"x" * 2048)
            images = {"outro": outro, "prayer": folder / "absent-prayer.jpg"}
            timeline = {
                "template": "micro_story", "voice_seconds_measured": 23.7,
                "identity_events": [
                    {"kind": "hook", "start": 0, "end": 5},
                    {"kind": "post_hook_silence", "start": 5, "end": 5.25},
                    {"kind": "topic", "start": 5.25, "end": 21},
                    {"kind": "outro", "start": 21.5, "end": 23.7},
                    {"kind": "final_silence", "start": 21, "end": 23.7},
                ],
            }
            with (
                mock.patch.object(timeline_render, "identity_asset_paths", return_value=images),
                mock.patch.object(timeline_render.subprocess, "run") as run,
            ):
                timeline_render._render_short_identity(
                    folder / "body.mp4", folder / "rendered.mp4", timeline=timeline
                )
            command = run.call_args.args[0]
            self.assertEqual(command.count("-i"), 2)  # body + original subscribe outro
            self.assertNotIn(str(images["prayer"]), command)
            self.assertIn(str(outro), command)
            self.assertIn("-filter_complex", command)


if __name__ == "__main__":
    unittest.main()

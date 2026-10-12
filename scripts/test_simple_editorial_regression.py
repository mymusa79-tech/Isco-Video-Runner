"""Small regression checks for Simple Mode editorial presentation (no paid calls)."""
from __future__ import annotations

import json
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clean_v2 import media
from clean_v2.cover_lite import select_cover_source
from clean_v2.short_timed_text import build_rich_ass, _font_size_for_event, TimedTextEvent
from clean_v2.simple_mode import SIMPLE_MODE_ENV


class SimpleEditorialRegressionTests(unittest.TestCase):
    def test_unrelated_pet_and_travel_stock_are_skipped(self):
        conflict = media._stock_metadata_context_conflict
        query = "person reflecting on career progress in a quiet room"
        self.assertTrue(conflict(query, "adorable golden retriever dog sleeping indoors"))
        self.assertTrue(conflict(query, "old world map with airline boarding pass travel tickets"))
        self.assertFalse(conflict("veterinarian caring for a dog", "golden retriever dog in clinic"))
        self.assertFalse(conflict("traveler planning route using world map", "world map and airline tickets"))
        self.assertFalse(conflict(query, ""))  # unknown metadata remains non-blocking

    def test_simple_caption_has_stable_smaller_cairo_type(self):
        events = [
            {"start": 0, "end": 1.5, "text": "لا تقارن نفسك بغيرك", "role": "hook"},
            {"start": 1.5, "end": 3, "text": "قارن نفسك بمن كنت أمس", "role": "beat"},
            {"start": 3, "end": 5, "text": "اختر خطوة صغيرة اليوم", "role": "payoff"},
        ]
        with mock.patch.dict(os.environ, {SIMPLE_MODE_ENV: "1"}):
            self.assertEqual(_font_size_for_event(TimedTextEvent(0, 1, "عمل", "hook")), 116)
            ass = build_rich_ass(events)
        self.assertIn("Style: Caption,Cairo,116,", ass)
        self.assertIn(",0,0,1,3,0,5,", ass)
        self.assertEqual(ass.count("Dialogue: "), 3)
        self.assertNotIn(r"\\fs166", ass)

    def test_quiet_source_pan_is_bounded_and_moving_stock_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "input.mp4"
            destination = Path(temp) / "output.mp4"
            with mock.patch.object(media, "probe_duration", return_value=4.0), mock.patch.object(media, "_run") as run, mock.patch.object(media, "_source_has_natural_motion", return_value=False) as motion:
                with mock.patch.dict(os.environ, {SIMPLE_MODE_ENV: "1"}):
                    media._trim_and_grade_clip(source, destination, width=1080, height=1920, seconds=4.0, grade_filter="")
                ffmpeg_filter = run.call_args.args[0][run.call_args.args[0].index("-vf") + 1]
                self.assertIn("min(t/4.000,1)", ffmpeg_filter)
                self.assertIn("crop=1080:1920", ffmpeg_filter)
                self.assertNotIn("zoompan", ffmpeg_filter)
                run.reset_mock()
                with mock.patch.dict(os.environ, {SIMPLE_MODE_ENV: "0"}):
                    media._trim_and_grade_clip(source, destination, width=1080, height=1920, seconds=4.0, grade_filter="")
                ffmpeg_filter = run.call_args.args[0][run.call_args.args[0].index("-vf") + 1]
                self.assertNotIn("min(t/4.000,1)", ffmpeg_filter)
                motion.return_value = True
                with mock.patch.dict(os.environ, {SIMPLE_MODE_ENV: "1"}):
                    media._trim_and_grade_clip(source, destination, width=1080, height=1920, seconds=4.0, grade_filter="")
                vf = run.call_args.args[0][run.call_args.args[0].index("-vf") + 1]
                self.assertNotIn("min(t/", vf)

    def test_simple_cover_drops_off_topic_with_existing_rights(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            visuals = root / "visuals"
            visuals.mkdir()
            for name in ("dog.mp4", "progress.mp4"):
                (visuals / name).write_bytes(b"x" * 2048)
            rights = {
                "assets": [
                    {"local_file": "dog.mp4", "section_id": "s1", "role": "hook",
                     "source_actual": "stock_motion",
                     "query": "person progressing at work",
                     "retrieval_description": "a dog resting on the floor"},
                    {"local_file": "progress.mp4", "section_id": "s1", "role": "body",
                     "source_actual": "stock_motion",
                     "visual_verification": "observed_matched",
                     "visual_evidence": {"source_sha256": hashlib.sha256(b"x" * 2048).hexdigest(), "prompt_hash": "p", "frame_sha256": ["f"]},
                     "query": "completed task notebook",
                     "retrieval_description": "a notebook containing completed task marks"},
                ]
            }
            (root / "rights-manifest.json").write_text(json.dumps(rights), encoding="utf-8")

            def choose(rows, *, fmt):
                assert fmt == "short"
                selected = max(rows, key=lambda row: row[0])
                return selected[1], selected[2], {
                    "combined_score": 1.0, "visual_score": 1.0,
                    "candidate_count_evaluated": len(rows),
                }

            with mock.patch.dict(os.environ, {SIMPLE_MODE_ENV: "1"}), mock.patch(
                "clean_v2.cover_studio.rank_cover_candidates", side_effect=choose
            ):
                selected, info = select_cover_source(root, fmt="short", section_id="s1")
            self.assertEqual(selected.name, "progress.mp4")
            self.assertEqual(info["selection"], "existing_visual_asset_cover_studio")


if __name__ == "__main__":
    unittest.main()

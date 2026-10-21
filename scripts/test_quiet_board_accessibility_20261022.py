"""Focused regression: quiet board grammar, speech cues, and accessible full Film SRT."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from clean_v2 import board_scene, film_captions


class QuietBoardTests(unittest.TestCase):
    def test_editorial_palette_is_restrained_without_erasing_the_accent(self):
        theme = board_scene._PALETTE["editorial"]
        self.assertLessEqual(theme["glow"], 0.20)
        self.assertLessEqual(theme["vignette"], 0.25)
        self.assertTrue(all(blob[3] <= 0.10 for blob in theme["blobs"]))
        self.assertEqual(theme["accents"][0], (201, 162, 75))
        self.assertLessEqual(board_scene.SETTLE_SECONDS, 1.5)
        self.assertIn("editorial", board_scene.THEMES)

    def test_phrase_time_used_only_when_verbatim_and_inside_scene(self):
        text = "قبل البداية نفعل الفعل ثم نرى النتيجة"
        items = [{"at_text": "الفعل"}, {"at_text": "النتيجة"}]
        clips = [
            {"text": "قبل البداية نفعل الفعل", "start": 100, "end": 104},
            {"text": "ثم نرى النتيجة", "start": 104, "end": 110},
        ]
        measured = board_scene.reveal_times(text, items, 10, speech_events=clips,
                                              absolute_scene_start=100)
        estimated = board_scene.reveal_times(text, items, 10)
        self.assertEqual(len(measured), 2)
        self.assertEqual(measured, sorted(measured))
        self.assertNotEqual(measured, estimated)
        missing = board_scene.reveal_times(
            text, items, 10,
            speech_events=[{"text": "unrelated", "start": 100, "end": 110}],
            absolute_scene_start=100,
        )
        self.assertEqual(missing, estimated)

    def test_ambiguous_repeated_speech_does_not_fabricate_alignment(self):
        text = "أولًا يحدث الأمر ثم يتكرر الأمر"
        items = [{"at_text": "الأمر"}]
        same = [
            {"text": "يحدث الأمر", "start": 20, "end": 23},
            {"text": "يتكرر الأمر", "start": 24, "end": 28},
        ]
        actual = board_scene.reveal_times(
            text, items, 8, speech_events=same, absolute_scene_start=20
        )
        expected = board_scene.reveal_times(text, items, 8)
        self.assertEqual(actual, expected)


class FullSpokenFilmSRTTests(unittest.TestCase):
    def test_board_suppresses_burned_overlay_not_accessibility_subtitles(self):
        events = [
            {"start": 0.0, "end": 2.0, "text": "بدأت الفكرة هنا."},
            {"start": 2.0, "end": 4.0, "text": "وهذه نقطة اللوحة."},
            {"start": 4.0, "end": 6.0, "text": "وهنا يكتمل المعنى."},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            final = root / "final.mp4"
            final.write_bytes(b"original")
            def fake_ffmpeg(args, **kwargs):
                Path(args[-1]).write_bytes(b"rendered")
                return SimpleNamespace(stderr="Cairo", returncode=0)
            with (
                mock.patch.object(film_captions, "build_events", return_value=(events, "measured")),
                mock.patch.object(board_scene, "blocked_windows", return_value=[(2.1, 3.9)]),
                mock.patch.object(film_captions.subprocess, "run", side_effect=fake_ffmpeg),
                mock.patch.object(film_captions, "resolved_font", return_value={"requested": "Cairo", "resolved": "Cairo"}),
            ):
                report = film_captions.apply_film_captions(output_dir=root, final_path=final)
            srt = (root / "film-captions.srt").read_text(encoding="utf-8")
            ass = (root / "film-captions.ass").read_text(encoding="utf-8")
            self.assertIn("وهذه نقطة اللوحة", srt)
            self.assertNotIn("وهذه نقطة اللوحة", ass)
            self.assertEqual(report["srt_event_count"], 3)
            self.assertEqual(report["event_count"], 2)
            self.assertEqual(report["board_suppressed_visual_cues"], 1)
            self.assertTrue(report["subtitles_keep_spoken_board_windows"])
            self.assertEqual(report["quality"]["cue_count"], 3)
            self.assertEqual(report["visual_caption_quality"]["cue_count"], 2)


if __name__ == "__main__":
    unittest.main()

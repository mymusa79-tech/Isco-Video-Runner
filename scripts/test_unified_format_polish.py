from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clean_v2 import media, short_timed_text
from clean_v2.cover_lite import select_cover_source
from clean_v2.identity_sequence import identity_timing_profile
from clean_v2.visual_story import CHANNEL_VISUAL_IDENTITY


class UnifiedFormatPolishTests(unittest.TestCase):
    def test_around_the_same_measured_voice_formats_keep_distinct_breathing(self):
        short = identity_timing_profile("short")
        film = identity_timing_profile("film")
        podcast = identity_timing_profile("podcast")
        self.assertLess(short["post_hook_silence_seconds"], film["post_hook_silence_seconds"])
        self.assertGreater(podcast["final_silence_seconds"], film["final_silence_seconds"])
        self.assertEqual(short["intro_silence_seconds"], 1.0)
        self.assertEqual(podcast["intro_silence_seconds"], 6.0)

    def test_arabic_diacritics_and_joining_controls_are_not_blindly_removed(self):
        source = "مُؤَثِّر\u200c ومطمئن\u061c، مع ٣٪"
        cleaned = short_timed_text._clean(source)
        self.assertIn("مُؤَثِّر\u200c", cleaned)
        self.assertIn("\u061c", cleaned)
        self.assertIn("٣٪", cleaned)
        diagnostic = short_timed_text.caption_character_diagnostics(
            ["تعلم □ 😀\ufe0f", source]
        )
        self.assertIn("U+25A1", diagnostic["removed_codepoints"])
        self.assertIn("U+1F600", diagnostic["removed_codepoints"])
        self.assertNotIn("U+200C", diagnostic["removed_codepoints"])

    def test_caption_one_line_is_preferred_and_two_lines_keep_all_words(self):
        with mock.patch.object(
            short_timed_text, "_caption_width",
            side_effect=lambda text, size: len(text) * size * 0.60,
        ):
            one, _ = short_timed_text._caption_rows_and_size(
                "خطوة صغيرة", fmt="short", font_size=166
            )
            self.assertEqual(one, ["خطوة صغيرة"])
            original = "رحلة جديدة نحو تحقيق فكرة واضحة وعملية اليوم"
            rows, size = short_timed_text._caption_rows_and_size(
                original, fmt="short", font_size=166
            )
            self.assertEqual(len(rows), 2)
            self.assertEqual(" ".join(rows), original)
            self.assertGreaterEqual(size, 118)

    def test_shared_format_color_strengths_are_gentler_without_blue_wash(self):
        sample = media._RgbStats(168, 139, 120, 34, 35, 32)
        target = media._channel_target_stats()
        short = media._reference_match_filter(sample, target, fmt="short")
        film = media._reference_match_filter(sample, target, fmt="film")
        podcast = media._reference_match_filter(sample, target, fmt="podcast")
        self.assertTrue(all(x.startswith("lutrgb=") for x in (short, film, podcast)))
        self.assertNotEqual(short, film)
        self.assertNotEqual(film, podcast)
        self.assertLess(media.MASTER_LOOK_FORMAT_STRENGTH["podcast"],
                        media.MASTER_LOOK_FORMAT_STRENGTH["short"])
        original = (0.75, 0.56, 0.40)
        full = media._master_look_value(*original)
        gentle = media._master_look_value(*original, strength=0.5)
        self.assertLess(
            sum(abs(x - y) for x, y in zip(gentle, original)),
            sum(abs(x - y) for x, y in zip(full, original)),
        )

    def test_missing_topic_image_gets_local_brand_not_generic_frame(self):
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            finished = root / "final.mp4"
            finished.write_bytes(b"final")
            source, report = select_cover_source(
                root, fmt="podcast", final_path=finished
            )
            self.assertEqual(report["selection"], "channel_identity_local_fallback")
            self.assertEqual(source.suffix, ".png")
            self.assertTrue(source.is_file())

    def test_film_and_podcast_caption_sidecars_are_optional_not_render_gates(self):
        timeline = {
            "status": "pass",
            "section_events": [
                {"section_id": "s1", "start": 0.0, "end": 2.0},
                {"section_id": "s2", "start": 3.0, "end": 9.0},
            ],
            "identity_events": [
                {"kind": "hook", "start": 0.0, "end": 2.0},
                {"kind": "prayer", "start": 2.0, "end": 3.0},
                {"kind": "topic", "start": 3.0, "end": 9.0},
            ],
        }
        script = {"sections": [
            {"id": "s1", "narration": "هذه بداية فكرة جديدة."},
            {"id": "s2", "narration": "يمكنك أن تبدأ خطوة واحدة، ثم تتابع التقدم بثبات."},
        ]}
        for fmt in ("film", "podcast"):
            with self.subTest(fmt=fmt), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                (root / "timeline-first.json").write_text(
                    json.dumps(timeline), encoding="utf-8"
                )
                with mock.patch.object(
                    short_timed_text, "_caption_width",
                    side_effect=lambda text, size: len(text) * size * .5,
                ):
                    result = short_timed_text.write_longform_caption_sidecars(
                        output_dir=root, script=script, fmt=fmt,
                    )
                self.assertEqual(result["status"], "available_optional")
                self.assertFalse(result["burned_into_final_video"])
                self.assertFalse(result["new_quality_gate"])
                srt = (root / result["srt"]).read_text(encoding="utf-8")
                ass = (root / result["ass"]).read_text(encoding="utf-8")
                self.assertIn("-->", srt)
                self.assertIn("فكرة جديدة", srt)
                self.assertNotIn("اللهم", srt)
                self.assertIn("WrapStyle: 2", ass)
                self.assertIn("Style: Caption,Cairo", ass)
                self.assertTrue((root / "caption-sidecars.json").is_file())

    def test_world_identity_is_global_and_respects_modest_people(self):
        self.assertIn("globally understandable", CHANNEL_VISUAL_IDENTITY)
        self.assertIn("modestly dressed woman", CHANNEL_VISUAL_IDENTITY)
        self.assertIn("no identifiable faces", CHANNEL_VISUAL_IDENTITY)


if __name__ == "__main__":
    unittest.main()

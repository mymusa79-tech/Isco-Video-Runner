from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import shutil
import subprocess

from clean_v2.film_captions import (
    FilmCaptionError, build_ass, build_srt, apply_film_captions,
    validate_caption_events, validate_srt_text, validate_ass_text, MAX_WORDS_PER_SECOND,
)
from clean_v2.podcast_captions import build_events
from clean_v2.timeline_render import render_identity_composition


class FilmCaptionsTests(unittest.TestCase):
    def test_film_caption_look_is_not_podcast_card(self) -> None:
        events = [
            {"start": 0.0, "end": 2.5, "text": "تفتح الهاتف بلا إشعار."},
            {"start": 2.5, "end": 5.0, "text": "لكن ما الذي تبحث عنه فعلًا؟"},
        ]
        ass = build_ass(events)
        self.assertIn("Style: Film,Cairo", ass)
        self.assertIn("\\fad(100,130)", ass)

        self.assertEqual(ass.count("Dialogue: 0,"), 2)
        self.assertNotIn("card,", ass)
        self.assertNotIn("shade,", ass)
        self.assertNotIn("\\p1", ass)
        self.assertIn("تفتح الهاتف", build_srt(events))

    def test_film_caption_is_large_enough_for_mobile(self) -> None:
        from clean_v2 import film_captions
        from clean_v2.short_timed_text import _CAPTION_FONT_MIN

        self.assertGreaterEqual(film_captions.FONT_SIZE, 88)
        self.assertGreaterEqual(_CAPTION_FONT_MIN["film"], 64)
        ass = build_ass([{"start": 0.0, "end": 2.5, "text": "تفتح الهاتف بلا إشعار."}])
        self.assertIn(f"Style: Film,Cairo,{film_captions.FONT_SIZE},", ass)
        self.assertIn("\\fs90", ass)

    def test_only_spoken_hook_and_topic_are_subtitled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "timeline-first.json").write_text(json.dumps({
                "status": "pass",
                "audio_units": [
                    {"section_id": "s1", "chunk": 1, "role": "hook",
                     "start": 0.0, "end": 3.0, "file": "missing.wav"},
                    {"section_id": "s1", "chunk": 2, "role": "prayer",
                     "start": 3.0, "end": 5.0, "file": "missing.wav"},
                    {"section_id": "s1", "chunk": 3, "role": "channel_identity",
                     "start": 5.0, "end": 8.0, "file": "missing.wav"},
                    {"section_id": "s1", "chunk": 4, "role": "topic",
                     "start": 8.0, "end": 12.0, "file": "missing.wav"},
                ],
            }), encoding="utf-8")
            (root / "voice-sections.json").write_text(json.dumps({
                "sections": [{"id": "s1", "chunks": [
                    {"chunk": 1, "text": "لماذا تمسك هاتفك رغم الصمت؟"},
                    {"chunk": 2, "text": "اللهم صل وسلم على نبينا محمد."},
                    {"chunk": 3, "text": "هنا نداء اليقظة."},
                    {"chunk": 4, "text": "العادة تبدأ قبل أن تسمع أي إشعار."},
                ]}],
            }, ensure_ascii=False), encoding="utf-8")
            events, mode = build_events(
                root, fmt="film", caption_roles=frozenset({"hook", "topic"})
            )
            self.assertGreaterEqual(len(events), 2)
            self.assertTrue(all(
                (float(e["end"]) <= 3.01 or float(e["start"]) >= 7.99)
                for e in events
            ))
            self.assertNotIn("اللهم", " ".join(e["text"] for e in events))
            self.assertNotIn("هنا نداء", " ".join(e["text"] for e in events))
            self.assertIn(mode, {
                "measured_audio_units_char_weighted",
                "measured_audio_units_snapped_to_pauses",
            })

    def test_film_render_does_not_draw_second_intro(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assets = {kind: root / f"{kind}.mp4" for kind in ("intro", "prayer", "outro")}
            for path in assets.values():
                path.write_bytes(b"asset" * 300)
            timeline = {
                "voice_seconds_measured": 23.0,
                "identity_events": [
                    {"kind": "hook", "start": 0.0, "end": 3.0},
                    {"kind": "prayer", "start": 3.35, "end": 5.3},
                    {"kind": "post_prayer_silence", "start": 5.3, "end": 5.75},
                    {"kind": "channel_identity", "start": 5.75, "end": 8.3},
                    {"kind": "pre_topic_silence", "start": 8.3, "end": 8.65},
                    {"kind": "topic", "start": 8.65, "end": 19.5},
                    {"kind": "outro", "start": 19.5, "end": 23.0},
                    {"kind": "final_silence", "start": 19.5, "end": 23.0},
                ],
            }
            with (
                mock.patch("clean_v2.timeline_render.identity_asset_paths", return_value=assets),
                mock.patch("clean_v2.timeline_render.subprocess.run") as runner,
            ):
                render_identity_composition(root / "source.mp4", root / "final.mp4",
                                            fmt="film", timeline=timeline)
            cmd = runner.call_args.args[0]
            graph = cmd[cmd.index("-filter_complex") + 1]
            self.assertIn("[0:v]null[v1]", graph)
            self.assertIn("[channel]", graph)
            self.assertNotIn("[intro]", graph)

REAL_RUN = Path(__file__).parent / "fixtures" / "film_real_run_v25_2"


def _real_events():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        for name in ("timeline-first.json", "voice-sections.json"):
            shutil.copy(REAL_RUN / name, root / name)
        events, _mode = build_events(root, fmt="film", caption_roles=frozenset({"hook", "topic"}))
    return events


class FilmCaptionQualityTests(unittest.TestCase):
    """Automatic proof on the measured timing of a real 11-minute Film (the V25.2 run)."""

    def test_real_run_cues_are_ordered_non_overlapping_and_readable(self) -> None:
        events = _real_events()
        stats = validate_caption_events(events, voice_seconds=662.51)
        self.assertGreater(stats["cue_count"], 150)
        self.assertLessEqual(stats["max_words_per_second"], MAX_WORDS_PER_SECOND)
        self.assertLessEqual(stats["max_words_per_second"], 4.5)
        validate_srt_text(build_srt(events), len(events))
        validate_ass_text(build_ass(events), len(events))

    def test_the_old_crammed_opening_is_rejected(self) -> None:
        # The shipped SRT put 16 cues into the first 4.6 seconds (24-30 words/second).
        crammed = [{"start": 0.1 * i, "end": 0.1 * i + 0.1, "text": "لماذا تفتح هاتفك للمرة الثالثة وأنت تحاول"}
                   for i in range(16)]
        with self.assertRaises(FilmCaptionError):
            validate_caption_events(crammed)
        fast = [{"start": 0.0, "end": 1.0, "text": "كلمة " * 7}]
        with self.assertRaisesRegex(FilmCaptionError, "reading_speed"):
            validate_caption_events(fast)

    def test_overlap_unsorted_and_outside_voice_are_rejected(self) -> None:
        ok = {"start": 0.0, "end": 2.0, "text": "جملة قصيرة هادئة"}
        with self.assertRaisesRegex(FilmCaptionError, "overlap"):
            validate_caption_events([ok, {"start": 1.5, "end": 4.0, "text": "جملة ثانية هادئة"}])
        with self.assertRaisesRegex(FilmCaptionError, "unsorted"):
            validate_caption_events([{"start": 5.0, "end": 7.0, "text": "جملة قصيرة هادئة"},
                                     {"start": 1.0, "end": 3.0, "text": "جملة ثانية هادئة"}])
        with self.assertRaisesRegex(FilmCaptionError, "outside_voice"):
            validate_caption_events([{"start": 10.0, "end": 14.0, "text": "جملة قصيرة هادئة"}], voice_seconds=9.0)
        with self.assertRaisesRegex(FilmCaptionError, "too_short"):
            validate_caption_events([{"start": 0.0, "end": 0.1, "text": "جملة"}])

    def test_malformed_sidecars_are_rejected(self) -> None:
        events = [{"start": 0.0, "end": 2.0, "text": "جملة قصيرة هادئة"},
                  {"start": 2.0, "end": 4.0, "text": "جملة ثانية هادئة"}]
        srt = build_srt(events)
        with self.assertRaises(FilmCaptionError):
            validate_srt_text(srt, 3)
        with self.assertRaises(FilmCaptionError):
            validate_srt_text(srt.replace("00:00:02,000 --> 00:00:04,000", "00:00:02.000 -> 00:00:04.000"), 2)
        with self.assertRaises(FilmCaptionError):
            validate_srt_text(srt.replace("2\n00:00:02", "5\n00:00:02"), 2)
        ass = build_ass(events)
        with self.assertRaises(FilmCaptionError):
            validate_ass_text(ass, 3)
        with self.assertRaises(FilmCaptionError):
            validate_ass_text(ass.replace(",Film,", ",Default,"), 2)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg required")
    def test_real_timing_burns_into_a_real_video_end_to_end(self) -> None:
        # Burns the real V25.2 cue list over a short test video: proves the render path runs, not just the parsers.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("timeline-first.json", "voice-sections.json"):
                shutil.copy(REAL_RUN / name, root / name)
            final = root / "final.mp4"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                            "-i", "color=c=0x223344:size=1920x1080:rate=30:duration=6",
                            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "6",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(final)],
                           check=True, timeout=120)
            report = apply_film_captions(output_dir=root, final_path=final)
            self.assertEqual(report["status"], "pass")
            self.assertLessEqual(report["quality"]["max_words_per_second"], MAX_WORDS_PER_SECOND)
            self.assertTrue(final.is_file() and final.stat().st_size > 0)
            self.assertTrue((root / "final-no-captions.mp4").is_file())
            self.assertTrue((root / "film-captions.srt").is_file())
            self.assertTrue((root / "film-captions.ass").is_file())


if __name__ == "__main__":
    unittest.main()

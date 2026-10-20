from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clean_v2 import film_captions
from clean_v2.short_timed_text import write_longform_caption_sidecars


class FilmCaptionStyleTests(unittest.TestCase):
    def test_film_typography_is_not_podcast_card(self) -> None:
        events = [
            {
                "start": 0.2, "end": 4.0,
                "text": "لماذا نواصل الطريق رغم أننا نعرف منذ البداية أنه لا يناسبنا وأنه يستهلك وقتنا؟",
                "role": "hook",
            },
            {
                "start": 5.0, "end": 7.5,
                "text": "عندما تتغير المعلومات علينا مراجعة القرار.",
                "role": "topic",
            },
        ]
        ass = film_captions.build_ass(events)
        self.assertIn("PlayResX: 1920", ass)
        self.assertIn("PlayResY: 1080", ass)
        self.assertIn("Style: Film,Cairo,", ass)
        self.assertIn(r"\an2\pos(960,892)", ass)
        self.assertIn(r"\fad(100,140)", ass)
        self.assertNotIn(r"\p1", ass)  # no Podcast card/rim/background vector
        self.assertNotIn("Dialogue: 1", ass)  # one text layer only
        self.assertIn("المعلومات", ass)
        self.assertEqual(ass.count("Dialogue: "), 2)
        self.assertIn("00:00:00,200 --> 00:00:04,000", film_captions.build_srt(events))

    def test_gold_reserved_for_two_row_hook(self) -> None:
        events = [
            {
                "start": 0.0, "end": 4.0, "role": "hook",
                "text": "أحيانًا نستمر في طريق نعرف أنه لم يعد يناسبنا لأننا نخشى أن نتراجع بعد كل ما دفعناه",
            },
            {
                "start": 5.0, "end": 9.0, "role": "topic",
                "text": "أحيانًا نستمر في طريق نعرف أنه لم يعد يناسبنا لأننا نخشى أن نتراجع بعد كل ما دفعناه",
            },
        ]
        ass = film_captions.build_ass(events)
        dialogue_lines = [line for line in ass.splitlines() if line.startswith("Dialogue: ")]
        self.assertEqual(len(dialogue_lines), 2)
        self.assertIn(film_captions.GOLD_ASS, dialogue_lines[0])
        self.assertNotIn(film_captions.GOLD_ASS, dialogue_lines[1])


class FilmCaptionTimingTests(unittest.TestCase):
    def test_only_hook_and_topic_events_and_board_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "timeline-first.json").write_text(
                json.dumps({
                    "identity_events": [
                        {"kind": "hook", "start": 0, "end": 8},
                        {"kind": "intro", "start": 8, "end": 11},
                        {"kind": "prayer", "start": 11, "end": 16},
                        {"kind": "topic", "start": 16, "end": 34},
                    ]
                }),
                encoding="utf-8",
            )
            original = [
                {"start": 1.0, "end": 4.0, "text": "هوك تجريبي"},
                {"start": 18.0, "end": 20.0, "text": "نص أثناء اللوحة"},
                {"start": 25.0, "end": 29.0, "text": "نص بعد اللوحة"},
            ]
            with (
                mock.patch.object(film_captions, "build_measured_events", return_value=(original, "measured_audio_units")),
                mock.patch.object(film_captions, "blocked_windows", return_value=[(17.0, 22.0)]),
            ):
                visible, timing, suppressed = film_captions.build_film_events(root)
            self.assertEqual(timing, "measured_audio_units")
            self.assertEqual(suppressed, 1)
            self.assertEqual(len(visible), 2)
            self.assertEqual([e["role"] for e in visible], ["hook", "topic"])

    def test_burned_film_sidecars_are_not_regenerated_from_section_estimates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "film-open-captions.srt").write_text(
                "1\n00:00:01,000 --> 00:00:03,000\nاختبار\n", encoding="utf-8"
            )
            (root / "film-key-text.json").write_text(
                json.dumps({
                    "status": "pass", "burned_into_final_video": True,
                    "event_count": 1, "timing": "measured_audio_units",
                }), encoding="utf-8"
            )
            report = write_longform_caption_sidecars(output_dir=root, script={}, fmt="film")
            self.assertEqual(report["srt"], "film-open-captions.srt")
            self.assertTrue(report["burned_into_final_video"])
            self.assertEqual(report["event_count"], 1)

    def test_podcast_builder_remains_separate(self) -> None:
        from clean_v2.podcast_captions import build_ass as podcast_build_ass

        events = [{"start": 0, "end": 3, "speaker": "A", "text": "لكن لماذا نفعل ذلك؟"}]
        ass = podcast_build_ass(events)
        self.assertIn("Dialogue: 1", ass)
        self.assertIn(r"\p1", ass)  # Podcast keeps its own card


if __name__ == "__main__":
    unittest.main()

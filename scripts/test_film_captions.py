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


    def test_film_identity_has_one_logo_only_during_spoken_definition(self) -> None:
        from clean_v2 import timeline_render

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assets = {kind: root / f"{kind}.mp4" for kind in ("intro", "prayer", "outro")}
            for path in assets.values():
                path.write_bytes(b"fixture" * 300)
            events = [
                {"kind": "hook", "start": 0.0, "end": 4.0},
                {"kind": "post_hook_silence", "start": 4.0, "end": 4.35},
                {"kind": "prayer", "start": 4.35, "end": 6.5},
                {"kind": "post_prayer_silence", "start": 6.5, "end": 6.75},
                {"kind": "channel_identity", "start": 6.75, "end": 10.0},
                {"kind": "pre_topic_silence", "start": 10.0, "end": 10.25},
                {"kind": "topic", "start": 10.25, "end": 35.0},
                {"kind": "outro", "start": 35.0, "end": 38.5},
                {"kind": "final_silence", "start": 35.0, "end": 38.5},
            ]
            with (
                mock.patch.object(timeline_render, "identity_asset_paths", return_value=assets),
                mock.patch.object(timeline_render.subprocess, "run") as run,
            ):
                timeline_render.render_identity_composition(
                    root / "base.mp4", root / "composed.mp4",
                    fmt="film", timeline={
                        "voice_seconds_measured": 38.5,
                        "identity_events": events,
                    },
                )
            command = run.call_args.args[0]
            filters = command[command.index("-filter_complex") + 1]
            self.assertIn("[0:v][prayer]overlay=0:0", filters)
            self.assertIn("[v2][channel]overlay=0:0", filters)
            self.assertNotIn("[0:v][intro]overlay", filters)
            self.assertNotIn("[v1][prayer]", filters)
            self.assertEqual(filters.count("[v2][channel]overlay"), 1)
            self.assertIn("gte(t,6.750)", filters)
            self.assertIn("lt(t,10.250)", filters)

    def test_stock_editor_action_does_not_duplicate_retrieval_query(self) -> None:
        from clean_v2.ready_production import build_story

        doc = {
            "title": "هاتف صامت",
            "template": "direct_cinematic",
            "script": {
                "sections": [
                    {"id": "s1", "narration": "لماذا تمسك هاتفك؟ ثم تتركه وتعود للعمل."},
                ],
            },
            "scenes": [
                {
                    "id": "a1", "section_id": "s1",
                    "asset": "assets/scene_01.jpg",
                    "start_text": "لماذا تمسك هاتفك؟",
                    "motion": "hold", "transition": "cut",
                    "stock": {
                        "query_en": "person reaches phone at desk",
                        "must_show": "hand reaching for phone, no visible face",
                    },
                },
                {
                    "id": "a2", "section_id": "s1",
                    "asset": "assets/scene_02.jpg",
                    "start_text": "ثم تتركه",
                    "motion": "hold", "transition": "cut",
                },
            ],
        }
        story = build_story(doc)
        beat = story["beats"][0]
        self.assertEqual(beat["stock_query_en"], "person reaches phone at desk")
        self.assertEqual(
            beat["semantic_must_have"],
            ["hand reaching for phone, no visible face"],
        )
        self.assertEqual(beat["shot_intent"], "hand reaching for phone, no visible face")



    def test_film_stock_plan_fails_fast_when_vision_budget_insufficient(self) -> None:
        from clean_v2.owner_pack import OwnerPackError
        from clean_v2.ready_production import ensure_film_stock_review_budget

        doc = {
            "format": "film",
            "scenes": [
                {"id": f"scene{i:02d}", "stock": {"query_en": "hand moves phone"}}
                for i in range(18)
            ],
        }
        with self.assertRaisesRegex(OwnerPackError, "18 مشهد ستوك"):
            ensure_film_stock_review_budget(doc, max_reviews=5)
        # One budgeted candidate is only an attempt, not an admitted clip.
        ensure_film_stock_review_budget(
            {**doc, "scenes": doc["scenes"][:5]}, max_reviews=5
        )
        # Podcast retains its independent 0–3 stock planning policy.
        ensure_film_stock_review_budget(
            {**doc, "format": "podcast"}, max_reviews=5
        )



if __name__ == "__main__":
    unittest.main()

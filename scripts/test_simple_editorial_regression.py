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
from clean_v2.cover_lite import select_cover_source, resolve_cover_text
from clean_v2.short_format import materialize_short_s3, short_prompt_context, normalize_short_practical_action
from clean_v2.short_timed_text import build_rich_ass, _font_size_for_event, TimedTextEvent
from clean_v2.simple_mode import SIMPLE_MODE_ENV


class SimpleEditorialRegressionTests(unittest.TestCase):
    def test_cover_copies_accepted_arabic_instead_of_run128_planner_phrase(self):
        plan = {"sections": [{"id": "s1", "cover_text": "الافتراض الخفي وراء الشعور بالخلف"}]}
        script = {"title": "لماذا تشعر بأنك متأخر عن الجميع؟"}
        with mock.patch.dict(os.environ, {SIMPLE_MODE_ENV: "1"}):
            self.assertEqual(resolve_cover_text(plan, section_id="s1", script=script), script["title"])

    def test_run131_cover_keeps_the_complete_question(self):
        # The accepted title and locked action are longer than the compact
        # preference. Run131 used to clip the seven-word planner heading at
        # five words and actually published "لماذا يتردد عقلك حتى في".
        plan = {"sections": [{"id": "s1", "heading": "لماذا يتردد عقلك حتى في القرارات البسيطة؟"}]}
        script = {
            "title": "شلل الاختيار: كيف تتغلب على تردد عقلك في القرارات الصغيرة",
            "sections": [{"id": "s3", "s3_locked_action": "حدد خيارًا واحدًا فقط قبل أن تبدأ في الاختيار."}],
        }
        with mock.patch.dict(os.environ, {SIMPLE_MODE_ENV: "1"}):
            self.assertEqual(
                resolve_cover_text(plan, section_id="s1", script=script),
                "لماذا يتردد عقلك حتى في القرارات البسيطة؟",
            )

    def test_simple_cover_does_not_use_an_unfinished_prepositional_phrase(self):
        plan = {"sections": [{"id": "s1", "heading": "لماذا يتردد عقلك حتى في"}]}
        script = {"title": "شلل الاختيار وكيفية فهم التردد في القرارات اليومية الصغيرة"}
        with mock.patch.dict(os.environ, {SIMPLE_MODE_ENV: "1"}):
            self.assertEqual(resolve_cover_text(plan, section_id="s1", script=script), script["title"])

    def test_simple_cover_can_fail_soft_instead_of_publishing_cut_off_copy(self):
        plan = {"sections": [{"id": "s1", "heading": "لماذا يتردد عقلك حتى في"}]}
        with mock.patch.dict(os.environ, {SIMPLE_MODE_ENV: "1"}):
            with self.assertRaisesRegex(ValueError, "simple_cover_no_complete_text"):
                resolve_cover_text(plan, section_id="s1", script={})

    def test_unrelated_pet_and_travel_stock_are_skipped(self):
        conflict = media._stock_metadata_context_conflict
        query = "person reflecting on career progress in a quiet room"
        self.assertTrue(conflict(query, "adorable golden retriever dog sleeping indoors"))
        self.assertTrue(conflict(query, "old world map with airline boarding pass travel tickets"))
        self.assertFalse(conflict("veterinarian caring for a dog", "golden retriever dog in clinic"))
        self.assertFalse(conflict("traveler planning route using world map", "world map and airline tickets"))
        self.assertFalse(conflict(query, ""))  # unknown metadata remains non-blocking

    def test_run131_s3_conditional_is_spoken_as_one_complete_sentence(self):
        action = "حدد خيارًا واحدًا فقط قبل أن تبدأ في الاختيار."
        payoff = "عندما تدرك أن التردد هو مجرد خوف من الندم."
        script = {"sections": [
            {"id": "s1", "narration": "هوك"},
            {"id": "s2", "narration": "الشرح"},
            {"id": "s3", "s3_payoff": payoff, "s3_locked_action": action},
        ]}
        materialize_short_s3(script)
        s3 = script["sections"][2]
        self.assertEqual(s3["s3_payoff"], payoff)
        self.assertEqual(s3["s3_locked_action"], action)
        self.assertEqual(s3["narration"],
                         "عندما تدرك أن التردد هو مجرد خوف من الندم، " + action)

    def test_completed_conditional_and_standalone_s3_unchanged(self):
        action = "حدد خيارًا واحدًا فقط قبل أن تبدأ في الاختيار."
        for payoff in (
            "عندما تزدحم الخيارات، تصبح المقارنة أصعب.",
            "المقارنة الطويلة قد تزيد التردد.",
        ):
            with self.subTest(payoff=payoff):
                script = {"sections": [
                    {"id": "s1", "narration": "هوك"},
                    {"id": "s2", "narration": "الشرح"},
                    {"id": "s3", "s3_payoff": payoff, "s3_locked_action": action},
                ]}
                materialize_short_s3(script)
                self.assertEqual(script["sections"][2]["narration"], payoff + " " + action)

    def test_run131_metadata_rejects_eyes_covering_prank_not_real_choice(self):
        query = "person overwhelmed by too many options with hands covering face close up"
        observed = "https://www.pexels.com/video/a-woman-covering-the-eyes-of-a-man-with-her-hands-6134334/"
        self.assertTrue(media._stock_metadata_context_conflict(query, observed))
        self.assertFalse(media._stock_metadata_context_conflict(
            "person intentionally covering the eyes of a man during a surprise", observed))
        self.assertFalse(media._stock_metadata_context_conflict(
            query, "hands hesitating between coffee and tea cups"))
        self.assertFalse(media._stock_metadata_context_conflict(query, ""))

    def test_short_planning_requests_payoff_visual_only_in_s3(self):
        instruction = short_prompt_context(
            {"approved_topic": "لماذا نتردد بين القهوة والشاي؟"},
            for_planning=True,
        )
        self.assertIn("Keep s2 visually unresolved", instruction)
        self.assertIn("Reuse hook objects", instruction)

    def test_run132_locked_action_agreement_before_voice(self):
        # Plan's exact phrase must be corrected before host-lock and TTS.
        raw = "اكتب ثلاث دروس تعلمتها من آخر خطأ ارتكبته"
        corrected = normalize_short_practical_action(raw)
        self.assertEqual(corrected, "اكتب ثلاثة دروس تعلمتها من آخر خطأ ارتكبته")
        self.assertEqual(
            normalize_short_practical_action("اكتب ثلاث كلمات عن آخر خطأ"),
            "اكتب ثلاث كلمات عن آخر خطأ",
        )

    def test_run132_payoff_and_action_make_complete_spoken_sentence(self):
        corrected = normalize_short_practical_action(
            "اكتب ثلاث دروس تعلمتها من آخر خطأ ارتكبته"
        )
        script = {"sections": [
            {"id": "s1", "narration": "هوك"},
            {"id": "s2", "narration": "الشرح"},
            {"id": "s3", "s3_payoff": "ومع وضوح الرؤية.", "s3_locked_action": corrected},
        ]}
        materialize_short_s3(script)
        closing = script["sections"][2]
        self.assertEqual(closing["s3_payoff"], "ومع وضوح الرؤية.")
        self.assertEqual(closing["s3_locked_action"], corrected)
        self.assertEqual(
            closing["narration"],
            "ومع وضوح الرؤية، اكتب ثلاثة دروس تعلمتها من آخر خطأ ارتكبته",
        )

    def test_run132_channel_only_does_not_fade_to_black(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "visual-06.mp4"
            with mock.patch.object(media, "_run") as ffmpeg_run:
                media.create_fallback_visual(target, portrait=True, beat={}, seconds=1.6)
            command = ffmpeg_run.call_args.args[0]
            source = command[command.index("-i") + 1]
            self.assertIn("color=c=#283a54:", source)
            self.assertIn("d=1.600", source)
            self.assertNotIn("fade=t=in", " ".join(command))
            self.assertNotIn("fade=t=out", " ".join(command))

    def test_simple_caption_has_stable_smaller_cairo_type(self):
        events = [
            {"start": 0, "end": 1.5, "text": "لا تقارن نفسك بغيرك", "role": "hook"},
            {"start": 1.5, "end": 3, "text": "قارن نفسك بمن كنت أمس", "role": "beat"},
            {"start": 3, "end": 5, "text": "اختر خطوة صغيرة اليوم", "role": "payoff"},
        ]
        with mock.patch.dict(os.environ, {SIMPLE_MODE_ENV: "1"}):
            self.assertEqual(_font_size_for_event(TimedTextEvent(0, 1, "عمل", "hook")), 144)
            ass = build_rich_ass(events)
        self.assertIn("Style: Caption,Cairo,144,", ass)
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

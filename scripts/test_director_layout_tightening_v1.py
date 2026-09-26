from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clean_v2 import media as media_module
from clean_v2 import short_audio_polish as audio_module
from clean_v2 import short_timed_text as text_module
from clean_v2 import visual_cta as cta_module
from clean_v2.identity_sequence import _asset_pair, identity_timing_profile
from clean_v2.music_library import load_catalog, select_music_track
from clean_v2.timeline_render import render_identity_composition
from clean_v2.visual_story import contextual_intent, validate_visual_story


class DirectorLayoutTighteningV1Tests(unittest.TestCase):
    def test_rule_1_hook_single_shot_is_capped_at_five_seconds(self) -> None:
        paths = [Path("hook-a.mp4"), Path("hook-b.mp4"), Path("body.mp4")]
        result_paths, durations = media_module._enforce_short_hook_shot_cap(
            paths,
            [8.0, 5.0, 7.0],
            ["s1", "s1", "s2"],
            hook_seconds=8.0,
        )
        self.assertEqual(result_paths, paths)
        self.assertLessEqual(durations[0], 5.0)
        self.assertAlmostEqual(sum(durations), 20.0)

    def test_rule_2_and_7_all_formats_are_fully_opaque_and_final_frame_freezes(self) -> None:
        for fmt in ("short", "film", "podcast"):
            with self.subTest(fmt=fmt), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                assets = {}
                for name in ("intro", "prayer", "outro"):
                    path = root / f"{name}.asset"
                    path.write_bytes(b"A" * 2048)
                    assets[name] = path
                timeline = {
                    "voice_seconds_measured": 12.0,
                    "identity_events": [
                        {"kind": "intro", "start": 2.0, "end": 3.0},
                        {"kind": "prayer", "start": 3.0, "end": 4.5},
                        {"kind": "outro", "start": 10.0, "end": 11.25},
                        {"kind": "final_silence", "start": 11.25, "end": 12.0},
                    ],
                }
                with mock.patch(
                    "clean_v2.timeline_render.identity_asset_paths", return_value=assets
                ), mock.patch("clean_v2.timeline_render.subprocess.run") as run:
                    render_identity_composition(
                        root / "source.mp4",
                        root / "destination.mp4",
                        fmt=fmt,
                        timeline=timeline,
                    )
                command = run.call_args.args[0]
                filters = command[command.index("-filter_complex") + 1]
                self.assertNotIn("colorchannelmixer=aa=", filters)
                self.assertNotIn("alpha=1", filters)
                self.assertIn("tpad=stop_mode=clone", filters)
                self.assertIn("between(t,2.000,3.000)", filters)
                self.assertIn("between(t,3.000,4.500)", filters)
                self.assertIn("between(t,10.000,12.000)", filters)

    def test_rule_7_format_asset_groups_are_distinct_and_timing_contract_is_shared(self) -> None:
        short_intro, short_outro, sw, sh = _asset_pair("short")
        film_intro, film_outro, fw, fh = _asset_pair("film")
        podcast_intro, podcast_outro, pw, ph = _asset_pair("podcast")
        self.assertEqual((sw, sh), (1080, 1920))
        self.assertEqual((fw, fh), (1920, 1080))
        self.assertEqual((pw, ph), (1920, 1080))
        self.assertEqual(short_intro.name, "short_intro.mp4")
        self.assertEqual(short_outro.name, "short_outro.mp4")
        self.assertEqual(film_intro.name, "long_intro.mp4")
        self.assertEqual(film_outro.name, "long_outro.mp4")
        self.assertEqual(podcast_intro.name, "podcast_intro.mp4")
        self.assertEqual(podcast_outro.name, "podcast_outro.mp4")
        self.assertEqual(len({short_intro.name, film_intro.name, podcast_intro.name}), 3)
        self.assertEqual(len({short_outro.name, film_outro.name, podcast_outro.name}), 3)
        for fmt in ("short", "film", "podcast"):
            profile = identity_timing_profile(fmt)
            self.assertGreater(profile["intro_silence_seconds"], 0)
            self.assertGreater(profile["final_silence_seconds"], 0)

    def test_rule_3_gold_keyword_is_materially_larger_than_white_body(self) -> None:
        events = [
            {"start": 0.0, "end": 2.0, "text": "القائمة أكبر مما تتوقع", "role": "hook"},
            {"start": 2.0, "end": 4.0, "text": "التردد يستهلك البداية", "role": "beat"},
            {"start": 4.0, "end": 6.0, "text": "مهمة واحدة تكفي", "role": "payoff"},
        ]
        ass = text_module.build_rich_ass(events)
        self.assertGreater(text_module.FOCUS_FONT_SIZE, text_module.BODY_FONT_SIZE)
        self.assertGreaterEqual(text_module.FOCUS_SCALE, 1.20)
        self.assertIn(text_module.ACCENT_ASS, ass)
        self.assertIn(r"\bord3", ass)
        self.assertIn("Style: Shadow", ass)
        self.assertEqual((text_module.CAPTION_SHADOW_X, text_module.CAPTION_SHADOW_Y), (4, 5))
        self.assertEqual((text_module.CAPTION_EXTRUDE_X, text_module.CAPTION_EXTRUDE_Y), (2, 3))
        self.assertIn(r"\fad(150,200)", ass)
        self.assertIn(r"\fscx99\fscy99", ass)

    def test_rule_3b_arabic_caption_uses_static_white_gold_line_hierarchy(self) -> None:
        events = [
            {
                "start": 0.0,
                "end": 3.0,
                "text": "لماذا يضيع وقتك دون أن تشعر كل يوم",
                "role": "hook",
            },
            {
                "start": 3.0,
                "end": 5.0,
                "text": "التشتت يسرق انتباهك بهدوء",
                "role": "beat",
            },
            {
                "start": 5.0,
                "end": 7.0,
                "text": "ابدأ بخطوة واحدة واضحة",
                "role": "payoff",
            },
        ]
        ass = text_module.build_rich_ass(events)
        self.assertIn(text_module.PRIMARY_ASS, ass)
        self.assertIn(text_module.ACCENT_ASS, ass)
        self.assertIn(r"\N", ass)
        self.assertIn(text_module.ARABIC_WORD_GAP, ass)
        self.assertIn(r"\fad(150,200)", ass)
        self.assertNotIn(r"\bord5", ass)

    def test_rule_4_context_requires_specific_meaning_before_general_mood(self) -> None:
        plan = {
            "sections": [{"id": "s1", "purpose": "ضغط قائمة المهام", "visual_query_en": "overwhelming task list desk"}]
        }
        story = validate_visual_story(
            {
                "schema_version": 2,
                "visual_world": "warm realistic desk",
                "story_arc": {"beginning": "pressure", "transformation": "focus", "arrival": "one task"},
                "retention_thread": {
                    "hook_tension": "too many tasks",
                    "payoff_answer": "one task becomes clear",
                    "visual_motif": "notebook",
                },
                "beats": [
                    {
                        "id": "b1", "section_id": "s1", "viewer_intent": "feel overload",
                        "meaning_target": "an overwhelming task list causing decision paralysis",
                        "semantic_must_have": ["many visible task marks", "hesitating hand"],
                        "semantic_should_avoid": ["generic coffee aesthetic"],
                        "shot_intent": "hand stalled above overloaded notebook",
                        "role": "hook", "stock_query_en": "overloaded notebook task list hesitant hand",
                        "source_preference": "ai_still",
                    },
                    {
                        "id": "b2", "section_id": "s1", "viewer_intent": "see one clear task",
                        "meaning_target": "one selected task breaks the paralysis",
                        "semantic_must_have": ["one visibly selected item"],
                        "semantic_should_avoid": ["generic laptop mood"],
                        "shot_intent": "same notebook with one task circled",
                        "role": "payoff", "stock_query_en": "one task circled notebook completed focus",
                        "source_preference": "ai_still",
                    },
                ],
            },
            plan,
        )
        context = contextual_intent(story, "b1", "")
        self.assertIn("Meaning:", context)
        self.assertIn("Must show:", context)
        self.assertIn("Avoid:", context)
        self.assertIn("specific meaning before mood", context)

    def test_rule_5_short_subscribe_cta_is_at_most_three_seconds(self) -> None:
        events = cta_module._events(
            fmt="short",
            duration=50.0,
            script={"title": "كيف تبدأ؟"},
            authored_mode="none",
        )
        combo = next(item for item in events if item.mode == "subscribe_combo")
        self.assertLessEqual(combo.end_seconds - combo.start_seconds, 3.0)
        source = inspect.getsource(cta_module._render)
        self.assertIn("warm", inspect.getsource(cta_module.apply_visual_cta_assets))
        self.assertIn("hue=h=38", source)

    def test_rule_5a_cta_action_differs_from_current_scene_action(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "timeline-first.json").write_text(
                json.dumps(
                    {
                        "section_events": [
                            {"section_id": "s1", "start": 0.0, "end": 30.0},
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (root / "visual-story.json").write_text(
                json.dumps(
                    {
                        "beats": [
                            {
                                "section_id": "s1",
                                "viewer_intent": "يرى فعل الكتابة بوضوح",
                                "meaning_target": "شخص يكتب ملاحظة قصيرة",
                                "shot_intent": "close shot of hand writing in notebook",
                                "semantic_must_have": ["pen", "notebook", "writing"],
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            original = [
                cta_module.VisualCtaEvent(
                    mode="comment",
                    start_seconds=12.0,
                    end_seconds=13.4,
                    x=465,
                    y=cta_module.SHORT_CTA_Y,
                    asset="comment_ORIGINAL.png",
                )
            ]
            revised, decisions = cta_module._enforce_semantic_separation(
                events=original,
                output_dir=root,
                script={
                    "sections": [
                        {"id": "s1", "narration": "اكتب الملاحظة التي ستبدأ بها الآن."}
                    ]
                },
                fmt="short",
            )
            self.assertNotEqual(revised[0].mode, "comment")
            self.assertIn(revised[0].mode, {"like", "share", "subscribe_combo"})
            self.assertTrue(decisions[0]["semantic_conflict_avoided"])

    def test_rule_5b_short_cta_sits_above_captions_and_sfx_between_voice_and_music(self) -> None:
        events = cta_module._events(
            fmt="short",
            duration=50.0,
            script={"title": "كيف تبدأ؟"},
            authored_mode="none",
        )
        self.assertTrue(events)
        self.assertTrue(all(item.y == cta_module.SHORT_CTA_Y for item in events))
        self.assertLess(cta_module.SHORT_CTA_Y, text_module.CAPTION_Y)
        self.assertEqual(cta_module.SFX_TARGET_REL_DB, -12.0)
        self.assertGreater(cta_module.SFX_TARGET_REL_DB, -22.0)
        self.assertGreaterEqual(cta_module.SFX_TARGET_REL_DB, cta_module.SFX_MIN_REL_DB)
        self.assertLessEqual(cta_module.SFX_TARGET_REL_DB, cta_module.SFX_MAX_REL_DB)

    def test_rule_5c_film_and_podcast_use_horizontal_cta_above_key_text(self) -> None:
        film = cta_module._events(
            fmt="film",
            duration=240.0,
            script={"title": "موضوع طويل"},
            authored_mode="comment",
        )
        podcast = cta_module._events(
            fmt="podcast",
            duration=240.0,
            script={"title": "حلقة خارج النص"},
            authored_mode="comment",
        )
        self.assertTrue(film)
        self.assertTrue(podcast)
        self.assertTrue(all(item.y == cta_module.HORIZONTAL_CTA_Y for item in film))
        self.assertTrue(all(item.y == cta_module.HORIZONTAL_CTA_Y for item in podcast))
        self.assertLess(cta_module.HORIZONTAL_CTA_Y, cta_module.HORIZONTAL_KEY_TEXT_Y)
        self.assertLessEqual(len(podcast), 2)
        self.assertLessEqual(len(podcast), len(film))
        for item in podcast:
            if item.mode == "subscribe_combo":
                self.assertEqual(item.x, 610)
            else:
                self.assertEqual(item.x, 908)

    def test_rule_6_captions_are_lower_center_above_bottom_15_percent(self) -> None:
        events = [
            {"start": 0.0, "end": 2.0, "text": "مهمة واحدة تكفي", "role": "hook", "section_id": "s1"},
            {"start": 2.0, "end": 4.0, "text": "خفف القائمة الآن", "role": "beat", "section_id": "s2"},
            {"start": 4.0, "end": 6.0, "text": "ابدأ بما أمامك", "role": "payoff", "section_id": "s3"},
        ]
        hint = text_module.build_composition_hints(events)[0]
        self.assertEqual(hint["zone"], "lower_center_youtube_safe")
        self.assertEqual(hint["x"], 540)
        self.assertLessEqual(hint["y"], int(1920 * 0.85) - 120)

    def test_rule_8_catalog_has_nine_cc0_tracks_and_topic_selection_is_deterministic(self) -> None:
        catalog = load_catalog()
        self.assertEqual(catalog["license"], "CC0-1.0 / public domain dedication")
        self.assertEqual(len(catalog["tracks"]), 9)
        fake_ready = {
            "source": "FreePD", "license": catalog["license"], "license_url": catalog["license_url"],
            "allow_download": False, "unavailable": [],
            "ready": [
                {**track, "path": f"/tmp/{track['filename']}", "cache_status": "hit"}
                for track in catalog["tracks"]
            ],
        }
        script = {"title": "لماذا تفشل خطط إدارة الوقت؟", "sections": []}
        with mock.patch("clean_v2.music_library.ensure_music_library", return_value=fake_ready):
            path, report = select_music_track(script, allow_download=False)
            repeated_path, repeated_report = select_music_track(
                script, fmt="short", allow_download=False
            )
            repeated_path_2, repeated_report_2 = select_music_track(
                script, fmt="short", allow_download=False
            )
        self.assertEqual(report["selected_id"], "calm-sketch-piano")
        self.assertIsNotNone(path)
        self.assertEqual(repeated_report["selected_id"], repeated_report_2["selected_id"])
        self.assertEqual(repeated_path, repeated_path_2)
        self.assertEqual(repeated_report["selection_family"], "focus")
        self.assertEqual(repeated_report["selection_format"], "short")
        self.assertIn(
            repeated_report["selected_id"],
            {"calm-sketch-piano", "acoustic-shifter", "wonder-flow"},
        )
        self.assertEqual(repeated_report["catalog_track_count"], 9)
        self.assertEqual((audio_module.MUSIC_MIN_REL_DB, audio_module.MUSIC_TARGET_REL_DB, audio_module.MUSIC_MAX_REL_DB), (-25.0, -22.0, -20.0))

    def test_rule_8b_music_studio_has_distinct_format_pools_without_provider_calls(self) -> None:
        catalog = load_catalog()
        fake_ready = {
            "source": "FreePD", "license": catalog["license"], "license_url": catalog["license_url"],
            "allow_download": False, "unavailable": [],
            "ready": [
                {**track, "path": f"/tmp/{track['filename']}", "cache_status": "hit"}
                for track in catalog["tracks"]
            ],
        }
        script = {"title": "أعتقد أنني بدأت أفوز أخيرًا", "sections": []}
        reports = {}
        with mock.patch("clean_v2.music_library.ensure_music_library", return_value=fake_ready):
            for fmt in ("short", "film", "podcast"):
                _path, reports[fmt] = select_music_track(
                    script, fmt=fmt, allow_download=False
                )
        self.assertEqual({reports[item]["selection_family"] for item in reports}, {"hopeful"})
        self.assertEqual({reports[item]["selection_format"] for item in reports}, {"short", "film", "podcast"})
        self.assertTrue(all(reports[item]["selected_id"] for item in reports))
        source = inspect.getsource(audio_module.apply_topic_audio_polish)
        self.assertIn("fmt=fmt", source)
        self.assertNotIn("provider", inspect.getsource(select_music_track).lower())

    def test_rule_8_music_window_is_exactly_topic_not_hook_identity_or_outro(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "timeline-first.json").write_text(
                json.dumps(
                    {
                        "identity_events": [
                            {"kind": "hook", "start": 0.0, "end": 4.0},
                            {"kind": "intro", "start": 4.0, "end": 5.0},
                            {"kind": "prayer", "start": 5.0, "end": 7.0},
                            {"kind": "channel_identity", "start": 7.0, "end": 10.0},
                            {"kind": "topic", "start": 10.0, "end": 28.0},
                            {"kind": "outro", "start": 28.0, "end": 30.0},
                            {"kind": "final_silence", "start": 30.0, "end": 30.75},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            for fmt in ("short", "film", "podcast"):
                with self.subTest(fmt=fmt):
                    self.assertEqual(audio_module._topic_window(root), (10.0, 28.0))

    def test_rule_9_noise_root_cause_is_removed_not_masked_by_mastering(self) -> None:
        source = inspect.getsource(audio_module)
        self.assertNotIn("anoisesrc=color=pink", source)
        self.assertNotIn("anoisesrc=color=brown", source)
        self.assertIn("post_master_procedural_noise_layer", source)
        self.assertIn("narration_mastering_untouched", source)
        self.assertIn("apply_topic_audio_polish", source)
        self.assertIn('fmt not in {"short", "film", "podcast"}', source)


if __name__ == "__main__":
    unittest.main()

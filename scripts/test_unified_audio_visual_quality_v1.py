from __future__ import annotations

import inspect
import unittest

from clean_v2 import (
    ai_still,
    contextual_cta,
    cover_studio,
    media,
    podcast_key_text,
    short_timed_text,
    tone_audit,
)
from clean_v2.identity_sequence import identity_timing_profile
from clean_v2.music_library import DIALOGUE_BED_TRACKS, _FORMAT_POOLS
from clean_v2.pipeline import _planning_prompt, _script_prompt
from clean_v2.visual_qa import BEST_AVAILABLE_PRIMARY_SEMANTIC_FLOOR
from clean_v2.visual_story import CHANNEL_VISUAL_IDENTITY


def _brief(fmt: str) -> dict:
    return {
        "format": fmt,
        "approved_topic": "لماذا نفشل في تنفيذ ما نخطط له",
        "topic": "لماذا نفشل في تنفيذ ما نخطط له",
        "audience": "جمهور عربي عام",
        "goal": "فهم الفكرة وتطبيق خطوة عملية",
        "tone": "هادئ وواضح",
        "constraints": [],
        "research_pack": [],
    }


def _plan() -> dict:
    return {
        "title": "اختبار",
        "promise": "فهم سبب واحد واضح",
        "cta": "",
        "sections": [
            {
                "id": "s1",
                "heading": "البداية",
                "purpose": "إظهار التوتر بوضوح",
                "cover_text": "لماذا نتوقف؟",
                "visual_query_en": "unfinished task on desk",
            },
            {
                "id": "s2",
                "heading": "السبب",
                "purpose": "شرح السبب",
                "cover_text": "السبب الخفي",
                "visual_query_en": "phone interrupting focused work",
            },
        ],
    }


class UnifiedAudioVisualQualityV1Tests(unittest.TestCase):
    def test_identity_breathing_is_format_aware_and_long_form_is_visible(self) -> None:
        short = identity_timing_profile("short")
        film = identity_timing_profile("film")
        podcast = identity_timing_profile("podcast")
        self.assertEqual(short["intro_silence_seconds"], 1.15)
        self.assertEqual(film["intro_silence_seconds"], 2.20)
        self.assertEqual(podcast["intro_silence_seconds"], 2.20)
        self.assertGreaterEqual(film["final_silence_seconds"], 3.5)
        self.assertGreaterEqual(podcast["final_silence_seconds"], 3.5)

    def test_music_pools_are_instrumental_dialogue_bed_only(self) -> None:
        self.assertTrue(DIALOGUE_BED_TRACKS)
        self.assertNotIn("all-in-silver-line", DIALOGUE_BED_TRACKS)
        for format_pools in _FORMAT_POOLS.values():
            for pool in format_pools.values():
                self.assertTrue(set(pool).issubset(DIALOGUE_BED_TRACKS))

    def test_text_overlays_have_no_black_panel_and_prayer_is_reserved(self) -> None:
        cta_source = inspect.getsource(contextual_cta)
        self.assertNotIn("drawbox=x=1000", cta_source)
        self.assertIn("Style: CTA,Cairo", cta_source)
        prayer = "اللهم صلِّ وسلِّم على نبينا محمد."
        self.assertTrue(short_timed_text._contains_prayer_text(prayer))
        self.assertTrue(podcast_key_text._contains_prayer_text(prayer))

    def test_video_text_is_cairo_bold_box_free_and_two_lines_max(self) -> None:
        self.assertEqual(short_timed_text.BODY_FONT, "Cairo")
        self.assertEqual(short_timed_text.MAX_CAPTION_LINES, 2)
        short_ass = short_timed_text.build_rich_ass(
            [
                {"start": 0.0, "end": 2.0, "text": "لماذا تعود إلى نفس النقطة كل مرة", "role": "hook"},
                {"start": 2.0, "end": 4.0, "text": "السبب ليس ضعفك بل طريقة البداية", "role": "beat"},
                {"start": 4.0, "end": 6.0, "text": "ابدأ بحركة واحدة واضحة فقط", "role": "payoff"},
            ]
        )
        self.assertIn("Style: Caption,Cairo", short_ass)
        self.assertNotIn("BorderStyle,3", short_ass)
        self.assertLessEqual(short_ass.count(r"\N"), 3)

        for fmt in ("film", "podcast"):
            ass = podcast_key_text.build_ass(
                [
                    {"start": 0.0, "end": 3.0, "text": "لماذا تعود إلى نفس النقطة كل مرة", "role": "hook"},
                    {"start": 4.0, "end": 7.0, "text": "هنا يبدأ التحول الحقيقي بهدوء", "role": "payoff"},
                ],
                fmt=fmt,
            )
            self.assertIn("Style: Caption,Cairo", ass)
            self.assertIn(r"\fs", ass)
            dialogue = [line for line in ass.splitlines() if line.startswith("Dialogue:")]
            self.assertTrue(dialogue)
            self.assertTrue(all(line.count(r"\N") <= 1 for line in dialogue))

    def test_real_stock_motion_never_restarts_or_gets_back_and_forth_fx(self) -> None:
        trim_source = inspect.getsource(media._trim_and_grade_clip)
        body_source = inspect.getsource(media._build_section_body_segments)
        self.assertNotIn("-stream_loop", trim_source)
        self.assertIn("tpad=stop_mode=clone", trim_source)
        self.assertNotIn("motion_mode=", body_source)
        self.assertNotIn('("push", "pan", "pull")', body_source)

    def test_cover_studio_uses_pinned_cairo_bold_black(self) -> None:
        self.assertEqual(cover_studio._FONT_NAMES["black"], ("Cairo.ttf",))
        self.assertEqual(cover_studio._FONT_NAMES["bold"], ("Cairo.ttf",))
        self.assertEqual(cover_studio._FONT_NAMES["medium"], ("Cairo.ttf",))
        source = inspect.getsource(cover_studio._resolve_font)
        self.assertIn('family = "Cairo"', source)

    def test_existing_tone_audit_rejects_shallow_copy_without_new_stage(self) -> None:
        scoped = inspect.getsource(tone_audit._scope_clean_v2_tone_prompt)
        self.assertIn("CONTENT DEPTH applies to Short, Film, and Podcast", scoped)
        self.assertIn('content_depth:', scoped)

    def test_dark_navy_channel_world_is_shared(self) -> None:
        self.assertIn("dark navy and charcoal", CHANNEL_VISUAL_IDENTITY)
        self.assertIn("warm gold only as a rare accent", CHANNEL_VISUAL_IDENTITY)
        self.assertIn("no blanket blue wash", CHANNEL_VISUAL_IDENTITY)

    def test_reference_color_prefers_channel_neutral_depth_over_warm_stock_medoid(self) -> None:
        measured = {
            "neutral.mp4": media._RgbStats(
                mean_r=141.2, mean_g=131.7, mean_b=119.7,
                std_r=76.0, std_g=75.0, std_b=80.5,
            ),
            "warm-beige.mp4": media._RgbStats(
                mean_r=179.7, mean_g=138.6, mean_b=108.4,
                std_r=56.4, std_g=57.5, std_b=54.7,
            ),
            "bright-warm.mp4": media._RgbStats(
                mean_r=184.0, mean_g=150.8, mean_b=120.3,
                std_r=43.0, std_g=42.7, std_b=40.4,
            ),
        }
        self.assertEqual(
            media._representative_reference(measured),
            "neutral.mp4",
        )
        self.assertGreater(media.COLOR_MATCH_STRENGTH, 0.55)
        self.assertLess(media.MASTER_LOOK_SATURATION, 0.875)

    def test_human_editorial_rhythm_and_voice_pacing_are_shared(self) -> None:
        for fmt in ("short", "film", "podcast"):
            planning = " ".join(_planning_prompt(_brief(fmt)).split())
            self.assertIn("HUMAN EDITORIAL RHYTHM applies to short, film, and podcast", planning)
            self.assertIn("establish -> detail/cutaway -> consequence/payoff", planning)
            script = " ".join(_script_prompt(_brief(fmt), _plan()).split())
            self.assertIn("8-22 Arabic words", script)
            self.assertIn("roughly 28 words", script)
        film_script = " ".join(_script_prompt(_brief("film"), _plan()).split())
        podcast_script = " ".join(_script_prompt(_brief("podcast"), _plan()).split())
        self.assertIn("12-24 Arabic words", film_script)
        self.assertIn("12-24 Arabic words", podcast_script)

    def test_podcast_has_the_same_free_ai_still_route_as_film(self) -> None:
        generator_source = inspect.getsource(ai_still.generate_cloudflare_ai_still)
        renderer_source = inspect.getsource(media._render_ai_still)
        self.assertIn('fmt in {"film", "podcast"}', generator_source)
        self.assertIn('fmt in {"film", "podcast"}', renderer_source)

    def test_generic_best_available_visual_floor_is_no_longer_070(self) -> None:
        self.assertEqual(BEST_AVAILABLE_PRIMARY_SEMANTIC_FLOOR, 0.78)


if __name__ == "__main__":
    unittest.main()

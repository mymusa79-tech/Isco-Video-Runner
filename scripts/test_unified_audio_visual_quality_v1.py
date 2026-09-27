from __future__ import annotations

import inspect
import unittest

from clean_v2 import (
    ai_still,
    contextual_cta,
    media,
    nabra_voice,
    podcast_key_text,
    short_timed_text,
    tone_audit,
)
from clean_v2.identity_sequence import identity_timing_profile
from clean_v2.music_library import DIALOGUE_BED_TRACKS, _FORMAT_POOLS
from clean_v2.nabra_voice import (
    NABRA_BATCH_SEAM_CROSSFADE_MS,
    NABRA_SPEED,
)
from clean_v2.pipeline import _planning_prompt, _script_prompt
from clean_v2.visual_qa import BEST_AVAILABLE_PRIMARY_SEMANTIC_FLOOR
from clean_v2.visual_story import CHANNEL_VISUAL_IDENTITY


def _brief(fmt: str) -> dict:
    return {
        "format": fmt,
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

    def test_nabra_keeps_reference_speed_without_artificial_batch_silence(self) -> None:
        self.assertEqual(NABRA_SPEED, 0.87)
        self.assertEqual(NABRA_BATCH_SEAM_CROSSFADE_MS, 18)
        source = inspect.getsource(nabra_voice)
        self.assertNotIn("NABRA_BATCH_SEAM_BREATH_MS", source)
        self.assertNotIn("NABRA_BATCH_SEAM_FADE_MS", source)
        self.assertIn("technical_batch_seam_silence_ms", source)

    def test_text_overlays_have_no_black_panel_and_prayer_is_reserved(self) -> None:
        cta_source = inspect.getsource(contextual_cta)
        self.assertNotIn("drawbox=x=1000", cta_source)
        self.assertIn("Style: CTA,Cairo", cta_source)
        prayer = "اللهم صلِّ وسلِّم على نبينا محمد."
        self.assertTrue(short_timed_text._contains_prayer_text(prayer))
        self.assertTrue(podcast_key_text._contains_prayer_text(prayer))

    def test_existing_tone_audit_rejects_shallow_copy_without_new_stage(self) -> None:
        scoped = inspect.getsource(tone_audit._scope_clean_v2_tone_prompt)
        self.assertIn("CONTENT DEPTH applies to Short, Film, and Podcast", scoped)
        self.assertIn('content_depth:', scoped)

    def test_dark_navy_channel_world_is_shared(self) -> None:
        self.assertIn("dark navy and charcoal", CHANNEL_VISUAL_IDENTITY)
        self.assertIn("warm gold only as a rare accent", CHANNEL_VISUAL_IDENTITY)
        self.assertIn("no blanket blue wash", CHANNEL_VISUAL_IDENTITY)

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

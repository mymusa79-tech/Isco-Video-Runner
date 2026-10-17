from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clean_v2 import media as media_module
from clean_v2 import visual_source_policy as policy
from scripts.test_clean_v2_visual_pacing import _candidate, _pacing_plan


def _beat(beat_id: str, preference: str, role: str = "body") -> dict:
    return {
        "id": beat_id,
        "section_id": "s1",
        "viewer_intent": f"understand {beat_id}",
        "shot_intent": f"hand closes laptop at night {beat_id}",
        "role": role,
        "stock_query_en": f"hand closes laptop at night {beat_id}",
        "source_preference": preference,
    }


def _story(*beats: dict) -> dict:
    return {
        "visual_world": "Grounded realism, natural color.",
        "story_arc": {"beginning": "a", "transformation": "b", "arrival": "c"},
        "retention_thread": {"hook_tension": "t", "payoff_answer": "p", "visual_motif": "laptop"},
        "beats": list(beats),
    }


def _fake_download(_url, destination):
    Path(destination).parent.mkdir(parents=True, exist_ok=True)
    Path(destination).write_bytes(b"V" * 4096)


def _fake_generate(*, prompt, destination, fmt, reference):
    Path(destination).parent.mkdir(parents=True, exist_ok=True)
    Path(destination).write_bytes(b"\xff\xd8\xff" + b"I" * 2048)
    return {
        "provider": "cloudflare_workers_ai",
        "model": "m",
        "prompt_sha256": "a" * 64,
        "source_url": "https://developers.cloudflare.com/workers-ai/",
        "ai_generated": True,
    }


def _fake_render(_source, destination, *, fmt):
    Path(destination).write_bytes(b"V" * 4096)
    return Path(destination)


def _fake_reference(_source, destination):
    Path(destination).write_bytes(b"R" * 2048)
    return Path(destination)


class PolicyTableTests(unittest.TestCase):
    def test_policy_per_series(self) -> None:
        self.assertEqual(policy.source_policy("short"), policy.STOCK_FIRST)
        self.assertEqual(policy.source_policy("film"), policy.STOCK_FIRST)
        self.assertEqual(policy.source_policy("podcast"), policy.EDITORIAL)
        self.assertEqual(policy.source_policy("short", "ala_mahl"), policy.APPROVED_AI_ONLY)

    def test_stock_first_overrides_planner_ai_and_still_but_not_podcast_or_owner_assets(self) -> None:
        for fmt in ("short", "film"):
            for preference in ("ai_still", "stock_still", "stock_motion"):
                self.assertEqual(
                    policy.effective_preference({"source_preference": preference}, fmt), "stock_motion"
                )
        self.assertEqual(policy.effective_preference({"source_preference": "ai_still"}, "podcast"), "ai_still")
        self.assertEqual(policy.effective_preference({"source_preference": "stock_still"}, "podcast"), "stock_still")
        approved = {"source_preference": "ai_still", "owner_asset": "a.png"}
        self.assertEqual(policy.effective_preference(approved, "short"), "ai_still")
        self.assertFalse(policy.ai_fallback_allowed(approved, "short"))
        self.assertFalse(policy.ai_fallback_allowed({}, "podcast"))
        self.assertTrue(policy.ai_fallback_allowed({}, "film"))
        self.assertFalse(policy.ai_fallback_allowed({}, "film", used=policy.MAX_AI_FALLBACKS))


class AcquisitionOrderTests(unittest.TestCase):
    def _run(self, fmt: str, beats: list[dict], *, stock_hits: bool):
        source = media_module.StockVisualSource()
        plan = _pacing_plan("s1")
        plan["visual_story"] = _story(*beats)
        counter = iter(range(1000))

        def pexels(_query, *, portrait):
            return _candidate("pexels", f"p{next(counter)}") if stock_hits else None

        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            source, "_pexels", side_effect=pexels
        ) as video, mock.patch.object(source, "_pixabay", return_value=None), mock.patch.object(
            source, "_coverr", return_value=None
        ), mock.patch.object(source, "_pexels_photo", return_value=None) as photo, mock.patch.object(
            source, "_pixabay_photo", return_value=None
        ), mock.patch.object(
            media_module, "_download_media", side_effect=_fake_download
        ), mock.patch.object(
            media_module, "_short_visual_color_compatible", return_value=(True, "ok")
        ), mock.patch(
            "clean_v2.ai_still.generate_cloudflare_ai_still", side_effect=_fake_generate
        ) as generate, mock.patch.object(
            media_module, "_render_ai_still", side_effect=_fake_render
        ), mock.patch.object(media_module, "_prepare_ai_reference", side_effect=_fake_reference):
            clips, rights = source.acquire(plan, Path(root), fmt, 5)
        return clips, rights, video, photo, generate

    def test_short_and_film_search_stock_video_before_any_ai(self) -> None:
        for fmt in ("short", "film"):
            with self.subTest(fmt=fmt):
                _c, rights, video, _p, generate = self._run(
                    fmt, [_beat("b1", "ai_still", "hook"), _beat("b2", "stock_still")], stock_hits=True
                )
                generate.assert_not_called()
                self.assertGreaterEqual(video.call_count, 2)
                self.assertEqual({row["source_actual"] for row in rights}, {"stock_motion"})

    def test_photos_are_tried_after_video_and_before_ai(self) -> None:
        _c, rights, video, photo, generate = self._run("film", [_beat("b1", "stock_motion")], stock_hits=False)
        video.assert_called()
        photo.assert_called()
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(rights[0]["source_actual"], "ai_still")
        self.assertEqual(rights[0]["source_policy"], "ai_fallback_after_stock")

    def test_ai_fallback_is_bounded(self) -> None:
        beats = [_beat(f"b{i}", "stock_motion") for i in range(1, 5)]
        _c, rights, _v, _p, generate = self._run("short", beats, stock_hits=False)
        self.assertEqual(generate.call_count, policy.MAX_AI_FALLBACKS)
        self.assertEqual(sum(1 for row in rights if row["source_actual"] == "ai_still"), policy.MAX_AI_FALLBACKS)

    def test_failed_ai_fallback_does_not_repeat_the_stock_search(self) -> None:
        from clean_v2 import ai_still as ai_still_module

        source = media_module.StockVisualSource()
        plan = _pacing_plan("s1")
        plan["visual_story"] = _story(_beat("b1", "stock_motion"))
        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            source, "_pexels", return_value=None
        ) as video, mock.patch.object(source, "_pixabay", return_value=None), mock.patch.object(
            source, "_coverr", return_value=None
        ), mock.patch.object(source, "_pexels_photo", return_value=None), mock.patch.object(
            source, "_pixabay_photo", return_value=None
        ), mock.patch(
            "clean_v2.ai_still.generate_cloudflare_ai_still",
            side_effect=ai_still_module.CloudflareAIStillUnavailable("cloudflare_image_preflight_timeout"),
        ) as generate:
            baseline = None
            source.acquire(plan, Path(root), "film", 5)
            baseline = video.call_count
        self.assertEqual(generate.call_count, 1)
        # Primary + one alternate query only: the failed fallback adds no more searches.
        self.assertLessEqual(baseline, 2)

    def test_podcast_keeps_the_planned_ai_first_choice(self) -> None:
        _c, rights, video, _p, generate = self._run("podcast", [_beat("b1", "ai_still", "hook")], stock_hits=True)
        self.assertEqual(generate.call_count, 1)
        video.assert_not_called()
        self.assertEqual(rights[0]["source_actual"], "ai_still")
        self.assertNotIn("source_policy", rights[0])


class RealismPromptTests(unittest.TestCase):
    def test_long_editorial_context_cannot_displace_the_scene_or_realism_rules(self) -> None:
        prompt = media_module._ai_still_prompt(
            {"visual_world": "force #0B1220 and #C9A24B", "retention_thread": {"visual_motif": "m " * 500}},
            {"role": "hook", "shot_intent": "hands repair a torn charcoal sleeve",
             "meaning_target": "detail " * 500, "viewer_intent": "intent " * 500,
             "semantic_must_have": ["material " * 500]}, fmt="short", with_reference=True)
        self.assertLessEqual(len(prompt), 2048)
        self.assertIn("hands repair a torn charcoal sleeve", prompt)
        self.assertIn("Use input image 0", prompt)
        self.assertNotIn("#0B1220", prompt)
        self.assertIn("No identifiable faces", prompt)
        self.assertTrue(prompt.endswith("exaggerated advertising look."))

    def test_ai_prompt_pins_natural_color_and_no_assumed_dress(self) -> None:
        prompt = media_module._ai_still_prompt(
            {"visual_world": "w", "retention_thread": {"visual_motif": "m"}},
            {"role": "body", "viewer_intent": "v", "shot_intent": "hand closes laptop"},
            fmt="film",
            with_reference=False,
        )
        self.assertLessEqual(len(prompt), 2048)
        self.assertIn("low-saturation", prompt)
        self.assertIn("no HDR", prompt)
        self.assertIn("No identifiable faces", prompt)
        self.assertIn("Gulf or traditional dress", prompt)
        self.assertNotIn("restrained deep navy", prompt)
        self.assertNotIn("warm-gold accents", prompt)
        for rule in ("Window light", "soft contrast", "plastic sheen", "artificial haze", "without a drawn band"):
            self.assertIn(rule, prompt)
        self.assertTrue(prompt.endswith("exaggerated advertising look."))


if __name__ == "__main__":
    unittest.main()

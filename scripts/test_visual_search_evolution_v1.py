from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clean_v2 import media as media_module
from clean_v2.media import StockVisualSource
from clean_v2.visual_story import validate_visual_story
from scripts.clean_v2_release_delivery import _provider_attribution_lines


class VisualSearchEvolutionTests(unittest.TestCase):
    def test_query_ladder_uses_one_real_situation_alternate(self) -> None:
        beat = {
            "stock_query_en": "person checking phone late at night",
            "stock_query_alt_en": "commuter reading job email on train",
            "semantic_must_have": ["phone", "unread work messages"],
        }
        ladder = media_module._stock_query_ladder(
            "person checking phone late at night",
            beat,
        )
        self.assertEqual(len(ladder), 2)
        self.assertEqual(ladder[0], "person checking phone late at night")
        self.assertEqual(ladder[1], "commuter reading job email on train")
        self.assertNotIn("cinematic", " ".join(ladder).lower())

    def test_coverr_search_is_ranked_locally_and_marks_attribution(self) -> None:
        response = {
            "hits": [
                {
                    "id": "c1",
                    "title": "Late night city skyline",
                    "description": "Wide city skyline at night",
                    "is_vertical": False,
                    "duration": 10,
                    "max_width": 1920,
                    "max_height": 1080,
                    "tags": ["city", "night"],
                    "urls": {"mp4_download": "https://cdn.coverr.co/videos/c1/download"},
                },
                {
                    "id": "c2",
                    "title": "Person checking phone at night",
                    "description": "Close view of hands checking phone in a dark room",
                    "is_vertical": True,
                    "duration": 8,
                    "max_width": 1080,
                    "max_height": 1920,
                    "search_keywords": ["phone", "night", "hands"],
                    "urls": {"mp4_download": "https://cdn.coverr.co/videos/c2/download"},
                },
            ]
        }
        source = StockVisualSource()
        with mock.patch.object(media_module, "_read_secret", return_value="coverr-key"), mock.patch.object(
            media_module, "_get_json", return_value=response
        ):
            result = source._coverr("hands checking phone night", portrait=True)
        self.assertIsNotNone(result)
        self.assertEqual(result["provider"], "coverr")
        self.assertEqual(result["asset_id"], "c2")
        self.assertTrue(result["attribution_required"])
        self.assertEqual(source._coverr_search_calls, 1)

    @staticmethod
    def _plan(*, preference: str = "stock_motion") -> dict:
        return {
            "sections": [
                {
                    "id": "s1",
                    "purpose": "show the visible tension",
                    "visual_query_en": "person checking phone late at night",
                    "visual_query_alt_en": "commuter reading job email on train",
                }
            ],
            "visual_story": {
                "beats": [
                    {
                        "id": "b1",
                        "section_id": "s1",
                        "viewer_intent": "show repeated checking",
                        "meaning_target": "visible repeated checking before sleep",
                        "semantic_must_have": ["hands checking phone at night"],
                        "semantic_should_avoid": ["generic desk"],
                        "shot_intent": "person checking phone late at night",
                        "stock_query_en": "person checking phone late at night",
                        "stock_query_alt_en": "commuter reading job email on train",
                        "role": "hook",
                        "source_preference": preference,
                    }
                ]
            },
        }

    @staticmethod
    def _write_fake_media(_url: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"x" * 2048)

    def test_hook_competes_all_three_motion_providers_and_uses_best_local_score(self) -> None:
        source = StockVisualSource()
        coverr_candidate = {
            "provider": "coverr",
            "asset_id": "c1",
            "download_url": "https://cdn.coverr.co/videos/c1/download",
            "source_url": "https://coverr.co",
            "creator": "Coverr",
            "creator_url": "https://coverr.co",
            "query": "person checking phone late at night",
            "media_kind": "video",
            "attribution_required": True,
            "local_rank_score": 0.71,
        }
        pexels_candidate = {
            "provider": "pexels",
            "asset_id": "p1",
            "download_url": "https://videos.pexels.com/video.mp4",
            "source_url": "https://www.pexels.com/video/1/",
            "creator": "Tester",
            "creator_url": "",
            "query": "person checking phone late at night",
            "local_rank_score": 0.93,
        }
        pixabay_candidate = {
            "provider": "pixabay",
            "asset_id": "x1",
            "download_url": "https://cdn.pixabay.com/video.mp4",
            "source_url": "https://pixabay.com/videos/1/",
            "creator": "Tester",
            "creator_url": "",
            "query": "person checking phone late at night",
            "local_rank_score": 0.82,
        }
        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            source, "_coverr", return_value=coverr_candidate
        ) as coverr, mock.patch.object(
            source, "_pexels", return_value=pexels_candidate
        ) as pexels, mock.patch.object(
            source, "_pixabay", return_value=pixabay_candidate
        ) as pixabay, mock.patch.object(
            media_module, "_download_media", side_effect=self._write_fake_media
        ), mock.patch.object(
            media_module, "_short_visual_color_compatible", return_value=(True, None)
        ):
            clips, rights = source.acquire(
                self._plan(),
                Path(root),
                "short",
                max_visuals=1,
            )
        self.assertEqual(len(clips), 1)
        self.assertEqual(rights[0]["provider"], "pexels")
        self.assertEqual(rights[0]["source_actual"], "stock_motion")
        self.assertTrue(rights[0]["provider_competition_used"])
        self.assertEqual(rights[0]["provider_competition_count"], 3)
        coverr.assert_called_once()
        pexels.assert_called_once()
        pixabay.assert_called_once()

    def test_body_keeps_cheap_coverr_first_sequential_policy(self) -> None:
        source = StockVisualSource()
        plan = self._plan()
        plan["visual_story"]["beats"][0]["role"] = "body"
        coverr_candidate = {
            "provider": "coverr",
            "asset_id": "c1",
            "download_url": "https://cdn.coverr.co/videos/c1/download",
            "source_url": "https://coverr.co",
            "creator": "Coverr",
            "creator_url": "https://coverr.co",
            "query": "person checking phone late at night",
            "media_kind": "video",
            "attribution_required": True,
            "local_rank_score": 0.60,
        }
        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            source, "_coverr", return_value=coverr_candidate
        ) as coverr, mock.patch.object(
            source, "_pexels", return_value=None
        ) as pexels, mock.patch.object(
            source, "_pixabay", return_value=None
        ) as pixabay, mock.patch.object(
            media_module, "_download_media", side_effect=self._write_fake_media
        ), mock.patch.object(
            media_module, "_short_visual_color_compatible", return_value=(True, None)
        ):
            _clips, rights = source.acquire(
                plan,
                Path(root),
                "short",
                max_visuals=1,
            )
        self.assertEqual(rights[0]["provider"], "coverr")
        self.assertFalse(rights[0]["provider_competition_used"])
        coverr.assert_called_once()
        pexels.assert_not_called()
        pixabay.assert_not_called()

    def test_second_query_is_used_only_after_first_query_sources_miss(self) -> None:
        source = StockVisualSource()
        pexels_candidate = {
            "provider": "pexels",
            "asset_id": "p2",
            "download_url": "https://videos.pexels.com/video.mp4",
            "source_url": "https://www.pexels.com/video/2/",
            "creator": "Tester",
            "creator_url": "",
            "query": "commuter reading job email on train",
        }
        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            source, "_coverr", side_effect=[None, None]
        ), mock.patch.object(
            source, "_pexels", side_effect=[None, pexels_candidate]
        ) as pexels, mock.patch.object(
            source, "_pixabay", return_value=None
        ), mock.patch.object(
            media_module, "_download_media", side_effect=self._write_fake_media
        ), mock.patch.object(
            media_module, "_short_visual_color_compatible", return_value=(True, None)
        ):
            _clips, rights = source.acquire(
                self._plan(),
                Path(root),
                "short",
                max_visuals=1,
            )
        self.assertEqual(len(pexels.call_args_list), 2)
        first_query = pexels.call_args_list[0].args[0]
        second_query = pexels.call_args_list[1].args[0]
        self.assertNotEqual(first_query, second_query)
        self.assertIn("commuter", second_query)
        self.assertTrue(rights[0]["query_evolution_used"])
        self.assertEqual(rights[0]["query_variant_index"], 1)

    def test_stock_still_uses_real_photo_before_motion(self) -> None:
        source = StockVisualSource()
        photo = {
            "provider": "pexels",
            "asset_id": "photo-1",
            "download_url": "https://images.pexels.com/photos/1/pexels-photo-1.jpeg",
            "source_url": "https://www.pexels.com/photo/1/",
            "creator": "Tester",
            "creator_url": "",
            "query": "hands holding unread letter",
            "media_kind": "photo",
        }

        def fake_render(_source: Path, destination: Path, *, fmt: str) -> Path:
            self.assertEqual(fmt, "short")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"v" * 2048)
            return destination

        plan = self._plan(preference="stock_still")
        plan["visual_story"]["beats"][0]["shot_intent"] = "hands holding unread letter"
        plan["visual_story"]["beats"][0]["stock_query_en"] = "hands holding unread letter"
        plan["visual_story"]["beats"][0]["stock_query_alt_en"] = "sealed envelope on quiet desk"
        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            source, "_pexels_photo", return_value=photo
        ) as pexels_photo, mock.patch.object(
            source, "_pixabay_photo", return_value=None
        ), mock.patch.object(
            source, "_coverr", return_value=None
        ) as coverr, mock.patch.object(
            source, "_pexels", return_value=None
        ) as pexels_video, mock.patch.object(
            source, "_pixabay", return_value=None
        ), mock.patch.object(
            media_module, "_download_media", side_effect=self._write_fake_media
        ), mock.patch.object(
            media_module, "_render_ai_still", side_effect=fake_render
        ), mock.patch.object(
            media_module, "_short_visual_color_compatible", return_value=(True, None)
        ):
            _clips, rights = source.acquire(
                plan,
                Path(root),
                "short",
                max_visuals=1,
            )
        pexels_photo.assert_called_once()
        coverr.assert_not_called()
        pexels_video.assert_not_called()
        self.assertEqual(rights[0]["source_actual"], "stock_still")
        self.assertEqual(rights[0]["media_kind"], "photo")

    def test_visual_story_accepts_real_stock_still_and_alt_query(self) -> None:
        plan = {
            "sections": [
                {
                    "id": "s1",
                    "purpose": "show the concrete moment",
                    "visual_query_en": "hands holding unread letter",
                    "cover_text": "لحظة التردد",
                }
            ]
        }
        story = {
            "visual_world": "grounded realistic interior",
            "story_arc": {
                "beginning": "hesitation",
                "transformation": "choice",
                "arrival": "clarity",
            },
            "beats": [
                {
                    "id": "b1",
                    "section_id": "s1",
                    "viewer_intent": "see the hesitation",
                    "meaning_target": "one unopened message creates hesitation",
                    "semantic_must_have": ["unopened letter"],
                    "semantic_should_avoid": ["generic laptop"],
                    "shot_intent": "hands holding unread letter in hallway",
                    "role": "hook",
                    "stock_query_en": "hands holding unread letter",
                    "stock_query_alt_en": "sealed envelope on quiet desk",
                    "display_text_ar": "قبل أن تفتحها",
                    "source_preference": "stock_still",
                }
            ],
        }
        validated = validate_visual_story(story, plan)
        beat = validated["beats"][0]
        self.assertEqual(beat["source_preference"], "stock_still")
        self.assertEqual(beat["stock_query_alt_en"], "sealed envelope on quiet desk")

    def test_coverr_attribution_is_added_when_used(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            Path(root, "rights-manifest.json").write_text(
                json.dumps(
                    {
                        "assets": [
                            {"provider": "coverr", "asset_id": "c1"},
                            {"provider": "pexels", "asset_id": "p1"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            lines = _provider_attribution_lines(Path(root))
        self.assertEqual(lines, ["Footage provided by Coverr: https://coverr.co"])


if __name__ == "__main__":
    unittest.main()

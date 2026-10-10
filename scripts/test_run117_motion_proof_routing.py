from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from clean_v2.media import StockVisualSource
from clean_v2.visual_qa import _recovery_source_preference
from clean_v2.visual_story import stock_scene_requires_motion

RUN117_DYNAMIC_SHOT = (
    "hands turning pages of a book with a highlighter marking "
    "a passage about cognitive"
)


class Run117MotionProofRoutingTests(unittest.TestCase):
    def test_page_turning_requires_motion_not_static_photo(self) -> None:
        self.assertTrue(stock_scene_requires_motion(RUN117_DYNAMIC_SHOT))
        self.assertTrue(stock_scene_requires_motion("hands flipping through book pages"))
        for static in ("open book on a desk", "person reading a book", "holding a completed puzzle piece"):
            self.assertFalse(stock_scene_requires_motion(static))

    def test_recovery_reuses_same_motion_policy_as_initial_retrieval(self) -> None:
        beat = {
            "source_preference": "stock_still",
            "shot_intent": RUN117_DYNAMIC_SHOT,
        }
        self.assertEqual(_recovery_source_preference(beat, "person reading a book"), "stock_motion")
        self.assertEqual(_recovery_source_preference(
            {"source_preference": "stock_still", "shot_intent": "open book on a desk"},
            "person reading a book",
        ), "stock_still")
        self.assertEqual(_recovery_source_preference(
            {"source_preference": "ai_still", "shot_intent": RUN117_DYNAMIC_SHOT},
            "person reading a book",
        ), "ai_still")

    def test_initial_selection_prefers_video_when_shot_proves_page_turning(self) -> None:
        source = StockVisualSource()
        candidate = {
            "provider": "pexels",
            "asset_id": "motion-1",
            "download_url": "https://example.org/one.mp4",
            "query": RUN117_DYNAMIC_SHOT,
            "local_rank_score": 0.9,
        }
        story = {"beats": [{
            "id": "b4",
            "section_id": "s2",
            "shot_intent": RUN117_DYNAMIC_SHOT,
            "stock_query_en": RUN117_DYNAMIC_SHOT,
            "source_preference": "stock_still",
            "role": "body",
        }]}
        plan = {
            "sections": [{"id": "s2", "visual_query_en": RUN117_DYNAMIC_SHOT}],
            "visual_story": story,
        }
        def fake_download(_url: str, destination: Path) -> None:
            Path(destination).write_bytes(b"fixture motion clip")
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(source, "_coverr", return_value=None) as coverr,
            patch.object(source, "_pexels", return_value=candidate) as pexels,
            patch.object(source, "_pixabay", return_value=None),
            patch.object(source, "_pexels_photo", side_effect=AssertionError("still chosen")) as photo,
            patch.object(source, "_pixabay_photo", side_effect=AssertionError("still chosen")),
            patch("clean_v2.media._download_media", side_effect=fake_download),
        ):
            paths, rights = source.acquire(plan, Path(tmp), fmt="film", max_visuals=1)
        self.assertEqual(len(paths), 1)
        self.assertEqual(rights[0]["source_actual"], "stock_motion")
        self.assertEqual(rights[0]["provider"], "pexels")
        self.assertEqual(photo.call_count, 0)
        self.assertEqual(coverr.call_count, 1)
        self.assertEqual(pexels.call_count, 1)


if __name__ == "__main__":
    unittest.main()

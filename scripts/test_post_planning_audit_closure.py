from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from isco_video_agent.visual_selection import MAX_VISION_REVIEWS_PER_SECTION

from scripts import provider_capacity_v2
from scripts.run123_budget_closure import SUCCESSFUL_ATTEMPT_ENVELOPES


class PixabayMandatoryCacheWindowTests(unittest.TestCase):
    def test_more_than_64_fresh_keys_are_not_evicted_before_24h(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = provider_capacity_v2.PersistentPixabaySearchCache(Path(tmp) / "cache.json")
            with patch.object(provider_capacity_v2.time, "time", return_value=1_000_000.0):
                for index in range(70):
                    result = cache.get_or_fetch(
                        provider="pixabay",
                        media_kind="video",
                        query=f"query {index}",
                        orientation="landscape",
                        per_page=12,
                        fetch=lambda index=index: [{"id": index}],
                    )
                    self.assertEqual(result, [{"id": index}])

                self.assertEqual(len(cache), 70)

                def must_not_refetch() -> list[dict]:
                    raise AssertionError("fresh 24h Pixabay cache key was evicted")

                first = cache.get_or_fetch(
                    provider="pixabay",
                    media_kind="video",
                    query="query 0",
                    orientation="landscape",
                    per_page=12,
                    fetch=must_not_refetch,
                )
                self.assertEqual(first, [{"id": 0}])
                self.assertEqual(cache.hits, 1)

    def test_expired_keys_are_still_pruned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = provider_capacity_v2.PersistentPixabaySearchCache(Path(tmp) / "cache.json")
            with patch.object(provider_capacity_v2.time, "time", return_value=1_000_000.0):
                cache.get_or_fetch(
                    provider="pixabay",
                    media_kind="video",
                    query="old",
                    orientation="landscape",
                    per_page=12,
                    fetch=lambda: [{"id": 1}],
                )
            later = 1_000_000.0 + provider_capacity_v2.PIXABAY_CACHE_TTL_SECONDS + 1
            with patch.object(provider_capacity_v2.time, "time", return_value=later):
                self.assertEqual(len(cache), 0)


class SharedVisionBudgetTests(unittest.TestCase):
    def test_runner_envelope_uses_engine_vision_ceiling(self) -> None:
        self.assertEqual(MAX_VISION_REVIEWS_PER_SECTION, 4)
        self.assertEqual(
            SUCCESSFUL_ATTEMPT_ENVELOPES["film"].vision,
            8 * MAX_VISION_REVIEWS_PER_SECTION,
        )
        self.assertEqual(
            SUCCESSFUL_ATTEMPT_ENVELOPES["story"].vision,
            5 * MAX_VISION_REVIEWS_PER_SECTION,
        )


if __name__ == "__main__":
    unittest.main()

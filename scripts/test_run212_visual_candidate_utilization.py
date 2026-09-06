from __future__ import annotations

import inspect
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from scripts import orchestration_media_port
from scripts import orchestration_shorts_port
from scripts import run212_visual_candidate_utilization as run212
from scripts import short_cinematic_director as director


RUN212_BEAT3_QUERY = (
    "cinematic shot organizing their morning routine calm sunlit "
    "person changing direction subtle action portrait vertical realistic cinematic"
)


class Run212VisualCandidateUtilizationTests(unittest.TestCase):
    def test_balanced_stock_query_preserves_late_beat_semantics(self) -> None:
        compact = run212._balanced_stock_query(RUN212_BEAT3_QUERY)
        tokens = compact.split()
        self.assertLessEqual(len(tokens), 8)
        self.assertIn("routine", tokens)
        self.assertIn("changing", tokens)
        self.assertIn("direction", tokens)
        self.assertIn("action", tokens)
        self.assertNotIn("cinematic", tokens)
        self.assertNotIn("portrait", tokens)

    def test_balanced_stock_query_keeps_historical_passthrough_when_transform_not_needed(self) -> None:
        query = "crossroads direction morning light"
        self.assertEqual(run212._balanced_stock_query(query), query)

    def test_short_beat_modifier_is_prioritized_before_base_query(self) -> None:
        primary, alternate = run212._beat_queries_with_priority_tail_preservation(
            "organizing morning routine calm sunlit",
            "why_reframe",
            2,
        )
        self.assertTrue(primary.startswith("person changing direction subtle action"))
        self.assertTrue(alternate.startswith("decisive action"))
        self.assertIn("organizing morning routine calm sunlit", primary)

    def test_severe_semantic_block_is_quarantined_across_later_beat_contexts(self) -> None:
        class BaseCache:
            def __init__(self) -> None:
                self.values = {}

            def set(self, provider, asset_id, ctx_hash, result) -> None:
                self.values[(provider, asset_id, ctx_hash)] = result

            def unavailable(self, provider, asset_id) -> bool:
                return False

        Cache = run212._hard_negative_cache_type(BaseCache)
        cache = Cache()
        cache.set(
            "pixabay",
            258799,
            "beat2",
            {
                "status": "block",
                "relevance": 0.1,
                "vision_review_performed": True,
                "semantic_verdict": True,
            },
        )
        self.assertTrue(cache.unavailable("pixabay", 258799))

    def test_technical_or_borderline_block_is_not_global_hard_negative(self) -> None:
        class BaseCache:
            def set(self, provider, asset_id, ctx_hash, result) -> None:
                pass

            def unavailable(self, provider, asset_id) -> bool:
                return False

        Cache = run212._hard_negative_cache_type(BaseCache)
        cache = Cache()
        cache.set(
            "pexels",
            1,
            "technical",
            {
                "status": "block",
                "relevance": 0.0,
                "vision_review_performed": False,
                "semantic_verdict": False,
                "verdict_authority": "technical_unavailable",
            },
        )
        cache.set(
            "pexels",
            2,
            "borderline",
            {
                "status": "block",
                "relevance": 0.7,
                "vision_review_performed": True,
                "semantic_verdict": True,
            },
        )
        self.assertFalse(cache.unavailable("pexels", 1))
        self.assertFalse(cache.unavailable("pexels", 2))

    def test_short_candidate_scope_is_bounded_and_restores_every_surface(self) -> None:
        original_beat_queries = director.beat_queries
        original_cache = director.VisualCandidateCache
        original_per_attempt = director.MAX_VISION_REVIEWS_PER_ATTEMPT
        original_per_beat = director.MAX_VISION_REVIEWS_PER_BEAT
        original_inspections = director.MAX_TOTAL_INSPECTIONS_PER_BEAT

        @contextmanager
        def no_run200(_root):
            yield

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            run212.run200,
            "short_vision_recovery_scope",
            no_run200,
        ):
            with run212.short_candidate_utilization_scope(Path(tmp)):
                self.assertEqual(
                    director.MAX_VISION_REVIEWS_PER_ATTEMPT,
                    run212.SHORT_VISION_REVIEWS_PER_ATTEMPT,
                )
                self.assertEqual(
                    director.MAX_VISION_REVIEWS_PER_BEAT,
                    run212.SHORT_VISION_REVIEWS_PER_BEAT,
                )
                self.assertEqual(
                    director.MAX_TOTAL_INSPECTIONS_PER_BEAT,
                    run212.SHORT_TOTAL_INSPECTIONS_PER_BEAT,
                )
                self.assertTrue(
                    getattr(director.VisualCandidateCache, "_isco_run212_hard_negative_cache", False)
                )
                self.assertIs(director.beat_queries, run212._beat_queries_with_priority_tail_preservation)

        self.assertIs(director.beat_queries, original_beat_queries)
        self.assertIs(director.VisualCandidateCache, original_cache)
        self.assertEqual(director.MAX_VISION_REVIEWS_PER_ATTEMPT, original_per_attempt)
        self.assertEqual(director.MAX_VISION_REVIEWS_PER_BEAT, original_per_beat)
        self.assertEqual(director.MAX_TOTAL_INSPECTIONS_PER_BEAT, original_inspections)

    def test_runtime_ports_activate_shared_and_short_scopes(self) -> None:
        media_source = inspect.getsource(orchestration_media_port.install_media_runtime_port)
        shorts_source = inspect.getsource(orchestration_shorts_port.prepare_authoritative_short_for_gold)
        self.assertIn("install_shared_visual_candidate_utilization", media_source)
        self.assertIn("short_candidate_utilization_scope", shorts_source)

    def test_shared_query_installer_is_idempotent(self) -> None:
        original = run212.opening_guard.stock_safe_search_query
        original_flag = run212._SHARED_INSTALLED
        try:
            run212._SHARED_INSTALLED = False
            run212.install_shared_visual_candidate_utilization()
            first = run212.opening_guard.stock_safe_search_query
            run212.install_shared_visual_candidate_utilization()
            self.assertIs(run212.opening_guard.stock_safe_search_query, first)
            self.assertTrue(getattr(first, "_isco_run212_balanced_stock_query", False))
        finally:
            run212.opening_guard.stock_safe_search_query = original
            run212._SHARED_INSTALLED = original_flag


if __name__ == "__main__":
    unittest.main()

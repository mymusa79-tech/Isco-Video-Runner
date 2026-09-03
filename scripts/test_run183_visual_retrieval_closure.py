from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

import isco_video_agent.visual_selection as visual_selection
from scripts import run183_visual_retrieval_closure as closure
from scripts import run183_visual_retrieval_scope_fix as scope_fix
from scripts import visual_retrieval_adjudication_v1 as v1


RUN183_VISUAL = "A person standing between two people, drawing a line with a marker, looking thoughtful"


def _candidate(asset_id: int, *, tags: str, url: str | None = None) -> dict:
    return {
        "id": asset_id,
        "url": url or f"https://www.pexels.com/video/{asset_id}/",
        "duration": 20,
        "video_files": [
            {"link": f"https://videos.pexels.com/video-{asset_id}.mp4", "width": 1280, "height": 720}
        ],
        "_isco_visual_intelligence": {"tags": tags},
    }


class Run183VisualRetrievalClosureTests(unittest.TestCase):
    def test_run183_query_family_preserves_relationship_boundary_semantics(self) -> None:
        family = closure.semantic_query_family(RUN183_VISUAL)
        self.assertIn("boundary", family.labels)
        self.assertIn("relationship", family.labels)
        self.assertTrue(family.alternates)
        self.assertLessEqual(len(family.alternates), closure.MAX_ALTERNATE_QUERY_FANOUT)
        joined = " ".join(family.alternates).lower()
        self.assertNotIn("marker closeup", joined)
        self.assertNotIn("closeup", joined)
        self.assertTrue(any("boundar" in item or "personal space" in item for item in family.alternates))
        self.assertTrue(any("conversation" in item or "relationship" in item for item in family.alternates))

    def test_semantic_ladder_uses_alternate_not_prop_closeup(self) -> None:
        ladder = closure.semantic_stock_query_ladder(RUN183_VISUAL)
        self.assertGreaterEqual(len(ladder), 2)
        self.assertNotIn("closeup", ladder[1].lower())
        self.assertNotEqual(ladder[1], "marker closeup")

    def test_refined_intent_lifts_personal_space_over_whiteboard_marker(self) -> None:
        refined = scope_fix._refine_run183_intent(closure._enriched_build_visual_intent)
        intent = refined(RUN183_VISUAL)
        self.assertEqual(intent.anchors, frozenset({"boundary", "relationship", "conversation", "space"}))
        self.assertNotIn("marker", intent.anchors)
        self.assertNotIn("line", intent.anchors)

        whiteboard = _candidate(34324862, tags="whiteboard marker drawing scribbles line")
        relationship = _candidate(800001, tags="personal boundaries relationship conversation personal space")
        whiteboard_score = v1._candidate_relevance("pexels", whiteboard, intent, 0, 2)
        relationship_score = v1._candidate_relevance("pexels", relationship, intent, 1, 2)
        self.assertGreater(relationship_score, whiteboard_score)

    def test_alternate_pool_excludes_run183_already_reviewed_asset(self) -> None:
        class Cache:
            def __init__(self) -> None:
                self._store = {}

        cache = Cache()
        calls: list[str] = []

        def alternate_query() -> str:
            return "personal boundaries calm conversation"

        def alternate_search(query: str) -> dict[str, list[dict]]:
            calls.append(query)
            if len(calls) == 1:
                return {
                    "pexels": [
                        _candidate(34324862, tags="whiteboard marker"),
                        _candidate(900001, tags="personal space conversation"),
                    ],
                    "pixabay": [_candidate(700001, tags="relationship boundaries")],
                }
            return {
                "pexels": [
                    _candidate(34324862, tags="whiteboard marker"),
                    _candidate(900002, tags="calm difficult conversation"),
                ],
                "pixabay": [_candidate(700001, tags="relationship boundaries")],
            }

        def fake_selector(*args, **kwargs):
            cache._store[("pexels", 34324862, "primary-context")] = {"status": "block"}
            query = kwargs["alternate_query_fn"]()
            return kwargs["alternate_search_fn"](query)

        wrapped = closure._wrap_selector(fake_selector, scope="single")
        result = wrapped(
            {"pexels": [], "pixabay": []},
            narration_context="healthy boundaries in relationships",
            intended_visual=RUN183_VISUAL,
            cache=cache,
            alternate_query_fn=alternate_query,
            alternate_search_fn=alternate_search,
        )
        pexels_ids = [item["id"] for item in result["pexels"]]
        pixabay_ids = [item["id"] for item in result["pixabay"]]
        self.assertNotIn(34324862, pexels_ids)
        self.assertEqual(len(pexels_ids), len(set(pexels_ids)))
        self.assertEqual(len(pixabay_ids), len(set(pixabay_ids)))
        self.assertLessEqual(len(calls), closure.MAX_ALTERNATE_QUERY_FANOUT)
        self.assertGreaterEqual(len(calls), 1)

    def test_current_selector_registry_records_cache_hit_review(self) -> None:
        reviewed: set[tuple[str, object]] = set()
        token = scope_fix._REVIEWED_CURRENT_SELECTOR.set(reviewed)
        try:
            review = visual_selection.CandidateReview(
                provider="pexels",
                candidate=_candidate(34324862, tags="whiteboard marker"),
                audit={"status": "block"},
                from_cache=True,
            )
            scope_fix._record_reviews(SimpleNamespace(reviewed=[review]))
            self.assertIn(("pexels", 34324862), reviewed)

            cache = SimpleNamespace(_store={})
            combined = scope_fix._reviewed_pairs_with_cache_fallback(
                lambda _cache, _before: set()
            )(cache, set())
            self.assertIn(("pexels", 34324862), combined)
        finally:
            scope_fix._REVIEWED_CURRENT_SELECTOR.reset(token)

    def test_query_ladder_is_historical_outside_production_and_semantic_inside(self) -> None:
        ladder = scope_fix._scoped_query_ladder(
            lambda _query: ("desk guitar", "guitar closeup"),
            lambda _query: ("desk guitar", "focus concentration notebook desk"),
        )
        with mock.patch.object(scope_fix, "_runtime_active", return_value=False):
            self.assertEqual(ladder("x"), ("desk guitar", "guitar closeup"))
        with mock.patch.object(scope_fix, "_runtime_active", return_value=True):
            self.assertEqual(ladder("x"), ("desk guitar", "focus concentration notebook desk"))

    def test_merge_deduplicates_same_asset_across_query_variants(self) -> None:
        pools = [
            (
                "personal boundaries calm conversation",
                {"pexels": [_candidate(1, tags="boundary"), _candidate(2, tags="conversation")]},
            ),
            (
                "personal space relationship conversation",
                {"pexels": [_candidate(1, tags="boundary"), _candidate(3, tags="relationship")]},
            ),
        ]
        merged = closure._merge_candidate_pools(pools, excluded_pairs=set())
        self.assertEqual([item["id"] for item in merged["pexels"]], [1, 2, 3])

    def test_existing_paid_vision_review_ceiling_is_not_increased(self) -> None:
        self.assertEqual(visual_selection.MAX_VISION_REVIEWS_PER_SECTION, 4)
        self.assertEqual(visual_selection.MAX_CANDIDATES_PER_VISUAL_ATTEMPT, 2)
        self.assertEqual(closure.MAX_ALTERNATE_QUERY_FANOUT, 2)

    def test_generic_query_family_never_collapses_to_closeup(self) -> None:
        family = closure.semantic_query_family("A hand placing a notebook beside a cup on a desk")
        self.assertTrue(family.primary)
        self.assertTrue(all("closeup" not in item.casefold() for item in family.alternates))


if __name__ == "__main__":
    unittest.main()

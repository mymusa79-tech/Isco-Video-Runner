from __future__ import annotations

import inspect
import unittest

import isco_video_agent.visual_selection as visual_selection
from scripts import canonical_v4_short_child
from scripts import orchestration_cinematic_port
from scripts import run183_visual_retrieval_closure as run183
from scripts import run214_canonical_visual_intent as run214
from scripts import run215_visual_fusion as run215
from scripts import visual_retrieval_adjudication_v1 as v1


class Run215VisualFusionTests(unittest.TestCase):
    def setUp(self) -> None:
        run215._VISION_NEGATIVES.set({})

    def tearDown(self) -> None:
        run215._VISION_NEGATIVES.set({})

    def test_dedup_preserves_all_query_provider_rank_sources(self) -> None:
        shared = {
            "id": 7,
            "url": "https://example.test/shared-7",
            "_isco_visual_intelligence": {"semantic_text": "coffee cup desk options"},
        }
        recovery_only = {
            "id": 8,
            "url": "https://example.test/recovery-8",
            "_isco_visual_intelligence": {"semantic_text": "hands choice desk"},
        }

        def base_merge(pools, *, excluded_pairs):
            del excluded_pairs
            # Match Run183 dedup behavior: keep the first object for a duplicate id.
            return {"pixabay": [shared, recovery_only]}

        merge = run215._merge_with_rank_provenance(base_merge)
        merged = merge(
            [
                ("middle aged tired", {"pixabay": [shared]}),
                ("hands choosing options desk", {"pixabay": [recovery_only, shared]}),
            ],
            excluded_pairs=set(),
        )
        meta = merged["pixabay"][0]["_isco_visual_intelligence"]
        sources = meta["retrieval_rank_sources_run215"]
        self.assertEqual(len(sources), 2)
        self.assertEqual(sources[0]["phase"], "primary")
        self.assertEqual(sources[0]["rank"], 1)
        self.assertEqual(sources[1]["phase"], "recovery")
        self.assertEqual(sources[1]["rank"], 2)
        self.assertEqual(
            meta["retrieval_queries_v2"],
            ["middle aged tired", "hands choosing options desk"],
        )

    def test_weighted_rrf_promotes_corrective_query_hit_over_generic_primary_hit(self) -> None:
        generic = {
            "id": 1,
            "url": "https://example.test/generic-1",
            "_isco_visual_intelligence": {
                "semantic_text": "middle aged man standing room",
                "global_retrieval_score_run214": 0.45,
                "retrieval_rank_sources_run215": [
                    {"query": "middle aged tired", "provider": "pexels", "rank": 1, "phase": "primary"}
                ],
            },
        }
        corrective = {
            "id": 2,
            "url": "https://example.test/corrective-2",
            "_isco_visual_intelligence": {
                "semantic_text": "coffee cup desk computer everyday options",
                "global_retrieval_score_run214": 0.41,
                "retrieval_rank_sources_run215": [
                    {"query": "hands choosing options desk", "provider": "pixabay", "rank": 1, "phase": "recovery"}
                ],
            },
        }
        intent = v1.build_visual_intent("choosing everyday options coffee desk")
        rerank = run215._rrf_feedback_rerank(lambda rows, _intent: list(rows))
        ranked = rerank([("pexels", generic), ("pixabay", corrective)], intent)
        self.assertEqual(ranked[0][1]["id"], 2)
        meta = ranked[0][1]["_isco_visual_intelligence"]
        self.assertGreater(meta["rrf_score_run215"], 0.5)
        self.assertIn("global_retrieval_score_run215", meta)

    def test_severe_vision_block_becomes_same_intent_negative_feedback(self) -> None:
        forest = {
            "id": 10,
            "url": "https://example.test/forest-10",
            "_isco_visual_intelligence": {
                "semantic_text": "hiking walking trail forest fog landscape",
                "global_retrieval_score_run214": 0.50,
                "retrieval_rank_sources_run215": [
                    {"query": "middle aged tired", "provider": "pixabay", "rank": 1, "phase": "primary"}
                ],
            },
        }
        coffee = {
            "id": 11,
            "url": "https://example.test/coffee-11",
            "_isco_visual_intelligence": {
                "semantic_text": "coffee cup desk computer options",
                "global_retrieval_score_run214": 0.43,
                "retrieval_rank_sources_run215": [
                    {"query": "hands choosing options desk", "provider": "pixabay", "rank": 2, "phase": "recovery"}
                ],
            },
        }
        canonical = "choosing everyday options coffee desk"

        def base_factory(audit_fn, _intent):
            return audit_fn

        def audit_fn(*args, **kwargs):
            del args, kwargs
            return {
                "status": "block",
                "relevance": 0.1,
                "visual_quality": 0.8,
                "vision_review_performed": True,
            }

        feedback_factory = run215._vision_feedback_audit_factory(base_factory)
        result = feedback_factory(audit_fn, canonical)(
            provider="pixabay",
            candidate=forest,
            narration_context="decision fatigue",
            intended_visual=canonical,
        )
        self.assertIn("vision_feedback_prototype_recorded_run215", result)

        intent = v1.build_visual_intent(canonical)
        rerank = run215._rrf_feedback_rerank(lambda rows, _intent: list(rows))
        ranked = rerank([("pixabay", forest), ("pixabay", coffee)], intent)
        self.assertEqual(ranked[0][1]["id"], 11)
        forest_meta = forest["_isco_visual_intelligence"]
        self.assertGreater(forest_meta["vision_negative_similarity_run215"], 0.0)

    def test_run215_is_shared_without_new_retry_provider_or_ai_budget(self) -> None:
        source = inspect.getsource(run215)
        port_source = inspect.getsource(orchestration_cinematic_port.install_cinematic_runtime_port)
        child_source = inspect.getsource(canonical_v4_short_child.execute)
        self.assertIn("install_run215_visual_fusion", port_source)
        self.assertIn("production.main()", child_source)
        self.assertNotIn("max_provider_attempts", source)
        self.assertNotIn("audit_video_preview(", source)
        self.assertNotIn("clip", source.casefold())
        self.assertNotIn("transformers", source.casefold())
        self.assertEqual(visual_selection.MAX_VISION_REVIEWS_PER_SECTION, 4)
        self.assertEqual(run183.MAX_ALTERNATE_QUERY_FANOUT, 2)
        self.assertIsInstance(run215.PRIMARY_STREAM_WEIGHT, float)
        self.assertIsInstance(run215.RECOVERY_STREAM_WEIGHT, float)
        self.assertLess(run215.PRIMARY_STREAM_WEIGHT, run215.RECOVERY_STREAM_WEIGHT)

    def test_run214_canonical_truth_owner_remains_unchanged(self) -> None:
        canonical = "a person choosing between everyday options"
        self.assertEqual(run214._canonical_judgment_intent(canonical, ("alternate",)), canonical)


if __name__ == "__main__":
    unittest.main()

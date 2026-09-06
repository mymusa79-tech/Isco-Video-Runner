from __future__ import annotations

import inspect
import unittest

import isco_video_agent.visual_selection as visual_selection
from scripts import canonical_v4_short_child
from scripts import orchestration_cinematic_port
from scripts import run183_visual_retrieval_closure as run183
from scripts import run212_visual_candidate_utilization as run212
from scripts import run214_canonical_visual_intent as run214
from scripts import run215_visual_fusion as run215
from scripts import visual_retrieval_adjudication_v1 as v1


class Run215VisualFusionIncidentTests(unittest.TestCase):
    """Regression for the real Run215 bounded-review candidate-utilization failure."""

    REVIEW_CEILING = 4

    def setUp(self) -> None:
        run215._VISION_NEGATIVES.set({})

    def tearDown(self) -> None:
        run215._VISION_NEGATIVES.set({})

    @staticmethod
    def _candidate(
        provider: str,
        candidate_id: int,
        semantic_text: str,
        score: float,
        rank: int,
        phase: str,
    ) -> tuple[str, dict]:
        return (
            provider,
            {
                "id": candidate_id,
                "url": f"https://example.test/{provider}/{candidate_id}",
                "_isco_visual_intelligence": {
                    "tags": semantic_text if provider == "pixabay" else "",
                    "semantic_text": semantic_text,
                    "global_retrieval_score_run214": score,
                    "retrieval_rank_sources_run215": [
                        {
                            "query": (
                                "hands choosing options desk"
                                if phase == "recovery"
                                else "person pausing before choosing"
                            ),
                            "provider": provider,
                            "rank": rank,
                            "phase": phase,
                        }
                    ],
                },
            },
        )

    def _run215_trace_fixture(self) -> list[tuple[str, dict]]:
        # Scores/order mirror the Run215 recovery trace. The useful Coffee Cup on Desk
        # candidate was globally seventh and therefore outside the four-review window.
        return [
            self._candidate("pexels", 36656155, "person man adult gesture thumbs up", 0.449921, 1, "primary"),
            self._candidate("pexels", 283000, "adventure backpack wilderness forest", 0.444163, 2, "primary"),
            self._candidate("pixabay", 44647, "old man senior portrait", 0.425497, 3, "primary"),
            self._candidate("pixabay", 45254, "old man newspaper reading", 0.427062, 4, "primary"),
            self._candidate("pixabay", 215751, "old man necktie portrait", 0.424763, 5, "primary"),
            self._candidate("pexels", 7918584, "guy man backpack outdoors", 0.417545, 6, "primary"),
            self._candidate("pixabay", 142370, "coffee cup on desk everyday choice", 0.410572, 3, "recovery"),
        ]

    def test_run215_incident_candidate_moves_inside_existing_four_review_window(self) -> None:
        rows = self._run215_trace_fixture()
        self.assertEqual(rows[6][1]["id"], 142370)
        self.assertNotIn(142370, [candidate["id"] for _provider, candidate in rows[: self.REVIEW_CEILING]])

        intent = v1.build_visual_intent("a man choosing between coffee and dates at a desk")
        rerank = run215._rrf_feedback_rerank(lambda items, _intent: list(items))
        ranked = rerank(rows, intent)

        review_window = [candidate["id"] for _provider, candidate in ranked[: self.REVIEW_CEILING]]
        self.assertIn(142370, review_window)
        self.assertEqual(len(review_window), self.REVIEW_CEILING)

    def test_primary_only_order_is_unchanged(self) -> None:
        rows = self._run215_trace_fixture()[:6]
        baseline_ids = [candidate["id"] for _provider, candidate in rows]
        intent = v1.build_visual_intent("a man choosing between coffee and dates at a desk")
        rerank = run215._rrf_feedback_rerank(lambda items, _intent: list(items))
        ranked = rerank(rows, intent)
        self.assertEqual([candidate["id"] for _provider, candidate in ranked], baseline_ids)
        self.assertFalse(run215._has_recovery_rank_evidence(ranked))

    def test_new_selector_audit_scope_clears_prior_attempt_hard_negatives(self) -> None:
        run215._VISION_NEGATIVES.set(
            {"same intent": (frozenset({"forest", "wilderness"}),)}
        )

        def base_factory(audit_fn, _intent):
            return audit_fn

        def audit_fn(*args, **kwargs):
            del args, kwargs
            return {
                "status": "pass",
                "relevance": 0.9,
                "visual_quality": 0.9,
                "vision_review_performed": True,
            }

        feedback_factory = run215._vision_feedback_audit_factory(base_factory)
        wrapped = feedback_factory(audit_fn, "fresh selector intent")
        self.assertEqual(run215._VISION_NEGATIVES.get(), {})
        self.assertEqual(wrapped(provider="pixabay", candidate={"id": 99})["status"], "pass")

    def test_multi_pool_recovery_keeps_same_attempt_negative_feedback(self) -> None:
        marker = {"same intent": (frozenset({"forest", "wilderness"}),)}
        run215._VISION_NEGATIVES.set(marker)
        primary = {"id": 1, "url": "https://example.test/1"}
        recovery = {"id": 2, "url": "https://example.test/2"}

        def base_merge(pools, *, excluded_pairs):
            del pools, excluded_pairs
            return {"pixabay": [primary, recovery]}

        merge = run215._merge_with_rank_provenance(base_merge)
        merge(
            [
                ("person pausing before choosing", {"pixabay": [primary]}),
                ("hands choosing options desk", {"pixabay": [recovery]}),
            ],
            excluded_pairs=set(),
        )
        self.assertEqual(run215._VISION_NEGATIVES.get(), marker)

    def test_severe_vision_block_penalizes_only_recovery_rerank(self) -> None:
        forest = self._candidate(
            "pixabay", 10, "hiking trail forest fog landscape", 0.50, 1, "primary"
        )[1]
        coffee = self._candidate(
            "pixabay", 11, "coffee cup desk computer everyday options", 0.43, 2, "recovery"
        )[1]
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
        rerank = run215._rrf_feedback_rerank(lambda items, _intent: list(items))
        ranked = rerank([("pixabay", forest), ("pixabay", coffee)], intent)
        self.assertEqual(ranked[0][1]["id"], 11)
        self.assertGreater(
            forest["_isco_visual_intelligence"]["vision_negative_similarity_run215"], 0.0
        )

    def test_review_budget_thresholds_and_retry_owners_are_unchanged(self) -> None:
        source = inspect.getsource(run215)
        port_source = inspect.getsource(orchestration_cinematic_port.install_cinematic_runtime_port)
        child_source = inspect.getsource(canonical_v4_short_child.execute)
        self.assertIn("install_run215_visual_fusion", port_source)
        self.assertIn("production.main()", child_source)
        self.assertEqual(run212.SHORT_VISION_REVIEWS_PER_BEAT, self.REVIEW_CEILING)
        self.assertEqual(run212.SHORT_VISION_REVIEWS_PER_ATTEMPT, 2)
        self.assertEqual(run183.MAX_ALTERNATE_QUERY_FANOUT, 2)
        self.assertNotIn("max_provider_attempts", source)
        self.assertNotIn("audit_video_preview(", source)
        self.assertNotIn("transformers", source.casefold())
        self.assertIsInstance(visual_selection.MAX_VISION_REVIEWS_PER_SECTION, int)
        self.assertLess(run215.PRIMARY_STREAM_WEIGHT, run215.RECOVERY_STREAM_WEIGHT)

    def test_run214_canonical_truth_owner_remains_unchanged(self) -> None:
        canonical = "a person choosing between everyday options"
        self.assertEqual(run214._canonical_judgment_intent(canonical, ("alternate",)), canonical)


if __name__ == "__main__":
    unittest.main()

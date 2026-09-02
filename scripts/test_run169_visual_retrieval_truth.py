from __future__ import annotations

import unittest
from types import SimpleNamespace

import isco_video_agent.opening_director as opening_director
import isco_video_agent.orchestrator as orchestrator
import isco_video_agent.section_visual_sequence as section_visual_sequence
import isco_video_agent.visual_selection as visual_selection
import scripts.opening_feasibility_guard as guard
from scripts.runtime_reliability import FailureClass, classify_failure


OBSERVED_RUN169_INTENT = (
    "person sitting at a desk with a guitar, looking contemplative, "
    "then picking up the guitar with a smile"
)


def _review(audit: dict) -> SimpleNamespace:
    return SimpleNamespace(audit=dict(audit))


def _failed(*audits: dict) -> SimpleNamespace:
    return SimpleNamespace(status="failed", reviewed=[_review(audit) for audit in audits])


def _selected(*audits: dict) -> SimpleNamespace:
    return SimpleNamespace(status="selected", reviewed=[_review(audit) for audit in audits])


def _technical(reason: str = "Vision provider call failed technically: APITimeoutError: timeout") -> dict:
    return {
        "status": "block",  # legacy selector transport sentinel only
        "reason": reason,
        "review_origin": guard._VISION_PROVIDER_FAILURE_ORIGIN,
        "vision_review_performed": False,
        "semantic_verdict": False,
        "verdict_authority": "technical_unavailable",
    }


def _semantic_block() -> dict:
    return {
        "status": "block",
        "reason": "semantic mismatch",
        "review_origin": "cloud_visual_qa",
        "vision_review_performed": True,
        "semantic_verdict": True,
    }


def _local_qr_block() -> dict:
    return {
        "status": "block",
        "reason": "qr_code_detected",
        "review_origin": "local_media_preflight",
        "vision_review_performed": False,
        "local_preflight_rejection": True,
    }


class Run169VisualRetrievalTruthTests(unittest.TestCase):
    def test_observed_directorial_query_becomes_bounded_stock_ladder(self) -> None:
        ladder = guard.stock_query_ladder(OBSERVED_RUN169_INTENT)
        self.assertEqual(ladder, ("desk guitar", "guitar closeup"))
        self.assertLessEqual(len(ladder), 2)
        self.assertNotIn("person", ladder[0])
        self.assertNotIn("then", ladder[0])
        self.assertNotIn("smile", ladder[0])

    def test_ladder_reuses_exactly_one_existing_alternate_slot_without_llm_call(self) -> None:
        calls = {"fallback": 0}

        def fallback() -> str:
            calls["fallback"] += 1
            return "another query"

        wrapped = guard._bounded_alternate_query_fn(fallback, OBSERVED_RUN169_INTENT)
        self.assertEqual(wrapped(), "guitar closeup")
        self.assertEqual(calls["fallback"], 0)

    def test_non_rewritten_query_keeps_existing_alternate_owner(self) -> None:
        calls = {"fallback": 0}

        def fallback() -> str:
            calls["fallback"] += 1
            return "ocean waves"

        wrapped = guard._bounded_alternate_query_fn(fallback, "ocean sunrise")
        self.assertEqual(wrapped(), "ocean waves")
        self.assertEqual(calls["fallback"], 1)

    def test_transient_envelope_is_explicitly_non_semantic(self) -> None:
        envelope = guard._vision_provider_failure_envelope(RuntimeError("APITimeoutError: timeout"))
        self.assertEqual(envelope["status"], "block")
        self.assertEqual(envelope["review_origin"], guard._VISION_PROVIDER_FAILURE_ORIGIN)
        self.assertIs(envelope["vision_review_performed"], False)
        self.assertIs(envelope["semantic_verdict"], False)
        self.assertEqual(envelope["verdict_authority"], "technical_unavailable")

    def test_two_technical_timeouts_are_not_candidate_exhaustion(self) -> None:
        result = _failed(_technical(), _technical("Vision provider call failed technically: timeout"))
        with self.assertRaisesRegex(guard.VisionVerdictUnavailableError, "VISION_UNAVAILABLE") as caught:
            guard._enforce_truthful_visual_outcome(result, scope="single")
        policy = classify_failure(caught.exception)
        self.assertIs(policy.failure_class, FailureClass.TRANSIENT_PROVIDER)
        self.assertEqual(policy.owner, "provider_router")

    def test_qr_blocks_plus_timeout_preserve_technical_unavailable_truth(self) -> None:
        result = _failed(_local_qr_block(), _technical())
        with self.assertRaisesRegex(guard.VisionVerdictUnavailableError, "semantic_verdict=false"):
            guard._enforce_truthful_visual_outcome(result, scope="single")

    def test_pure_semantic_or_local_blocks_remain_candidate_exhaustion_result(self) -> None:
        result = _failed(_local_qr_block(), _semantic_block(), _semantic_block())
        self.assertIs(guard._enforce_truthful_visual_outcome(result, scope="single"), result)

    def test_later_pass_wins_even_after_earlier_technical_unavailable(self) -> None:
        result = _selected(_technical(), {"status": "pass", "semantic_verdict": True})
        self.assertIs(guard._enforce_truthful_visual_outcome(result, scope="single"), result)

    def test_truth_rule_is_shared_across_short_and_long_visual_scopes(self) -> None:
        for scope in ("single", "opening", "section"):
            with self.subTest(scope=scope):
                result = _failed(_technical())
                with self.assertRaisesRegex(guard.VisionVerdictUnavailableError, f"scope={scope}"):
                    guard._enforce_truthful_visual_outcome(result, scope=scope)

    def test_stable_intent_is_preserved_for_vision_even_when_retrieval_broadens(self) -> None:
        seen: dict[str, str] = {}

        def audit_fn(**kwargs):
            seen["intended_visual"] = kwargs["intended_visual"]
            return {"status": "block"}

        wrapped = guard._stable_intent_audit(audit_fn, OBSERVED_RUN169_INTENT)
        wrapped(intended_visual="guitar closeup")
        self.assertEqual(seen["intended_visual"], OBSERVED_RUN169_INTENT)
        self.assertEqual(guard.stock_safe_search_query(OBSERVED_RUN169_INTENT), "desk guitar")

    def test_existing_visual_review_caps_are_unchanged(self) -> None:
        self.assertEqual(visual_selection.MAX_VISION_REVIEWS_PER_SECTION, 4)
        self.assertEqual(guard.MAX_ADAPTIVE_OPENING_REVIEWS, 8)
        self.assertEqual(guard.MAX_ADAPTIVE_SECTION_REVIEWS, 5)
        self.assertEqual(opening_director.MAX_OPENING_VISION_REVIEWS, 4)
        self.assertEqual(section_visual_sequence.MAX_SECTION_SEQUENCE_VISION_REVIEWS, 4)

    def test_no_provider_or_security_owner_is_replaced_by_run169(self) -> None:
        # Run169 changes retrieval syntax and terminal truth only. The stock provider
        # functions and Security/QR preflight remain owned by the existing runtime stack.
        self.assertEqual(guard.STOCK_CANDIDATE_POOL, 40)
        self.assertEqual(visual_selection.MAX_EXTRA_CANDIDATE_INSPECTIONS, 4)
        self.assertTrue(callable(orchestrator.pexels_search_videos))
        self.assertTrue(callable(orchestrator.pixabay_provider.search_videos))


if __name__ == "__main__":
    unittest.main()

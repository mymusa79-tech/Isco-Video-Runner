from __future__ import annotations

from types import SimpleNamespace

import pytest

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


def test_observed_directorial_query_becomes_bounded_stock_ladder() -> None:
    ladder = guard.stock_query_ladder(OBSERVED_RUN169_INTENT)
    assert ladder == ("desk guitar", "guitar closeup")
    assert len(ladder) <= 2
    assert "person" not in ladder[0]
    assert "then" not in ladder[0]
    assert "smile" not in ladder[0]


def test_ladder_reuses_exactly_one_existing_alternate_slot_without_llm_call() -> None:
    calls = {"fallback": 0}

    def fallback() -> str:
        calls["fallback"] += 1
        return "another query"

    wrapped = guard._bounded_alternate_query_fn(fallback, OBSERVED_RUN169_INTENT)
    assert wrapped() == "guitar closeup"
    assert calls["fallback"] == 0


def test_non_rewritten_query_keeps_existing_alternate_owner() -> None:
    calls = {"fallback": 0}

    def fallback() -> str:
        calls["fallback"] += 1
        return "ocean waves"

    wrapped = guard._bounded_alternate_query_fn(fallback, "ocean sunrise")
    assert wrapped() == "ocean waves"
    assert calls["fallback"] == 1


def test_transient_envelope_is_explicitly_non_semantic() -> None:
    envelope = guard._vision_provider_failure_envelope(RuntimeError("APITimeoutError: timeout"))
    assert envelope["status"] == "block"
    assert envelope["review_origin"] == guard._VISION_PROVIDER_FAILURE_ORIGIN
    assert envelope["vision_review_performed"] is False
    assert envelope["semantic_verdict"] is False
    assert envelope["verdict_authority"] == "technical_unavailable"


def test_two_technical_timeouts_are_not_candidate_exhaustion() -> None:
    result = _failed(_technical(), _technical("Vision provider call failed technically: timeout"))
    with pytest.raises(guard.VisionVerdictUnavailableError, match="VISION_UNAVAILABLE") as caught:
        guard._enforce_truthful_visual_outcome(result, scope="single")
    policy = classify_failure(caught.value)
    assert policy.failure_class is FailureClass.TRANSIENT_PROVIDER
    assert policy.owner == "provider_router"


def test_qr_blocks_plus_timeout_preserve_technical_unavailable_truth() -> None:
    result = _failed(_local_qr_block(), _technical())
    with pytest.raises(guard.VisionVerdictUnavailableError, match="semantic_verdict=false"):
        guard._enforce_truthful_visual_outcome(result, scope="single")


def test_pure_semantic_or_local_blocks_remain_candidate_exhaustion_result() -> None:
    result = _failed(_local_qr_block(), _semantic_block(), _semantic_block())
    assert guard._enforce_truthful_visual_outcome(result, scope="single") is result


def test_later_pass_wins_even_after_earlier_technical_unavailable() -> None:
    result = _selected(_technical(), {"status": "pass", "semantic_verdict": True})
    assert guard._enforce_truthful_visual_outcome(result, scope="single") is result


@pytest.mark.parametrize("scope", ["single", "opening", "section"])
def test_truth_rule_is_shared_across_short_and_long_visual_scopes(scope: str) -> None:
    result = _failed(_technical())
    with pytest.raises(guard.VisionVerdictUnavailableError, match=f"scope={scope}"):
        guard._enforce_truthful_visual_outcome(result, scope=scope)


def test_stable_intent_is_preserved_for_vision_even_when_retrieval_broadens() -> None:
    seen: dict[str, str] = {}

    def audit_fn(**kwargs):
        seen["intended_visual"] = kwargs["intended_visual"]
        return {"status": "block"}

    wrapped = guard._stable_intent_audit(audit_fn, OBSERVED_RUN169_INTENT)
    wrapped(intended_visual="guitar closeup")
    assert seen["intended_visual"] == OBSERVED_RUN169_INTENT
    assert guard.stock_safe_search_query(OBSERVED_RUN169_INTENT) == "desk guitar"


def test_existing_visual_review_caps_are_unchanged() -> None:
    assert visual_selection.MAX_VISION_REVIEWS_PER_SECTION == 4
    assert guard.MAX_ADAPTIVE_OPENING_REVIEWS == 8
    assert guard.MAX_ADAPTIVE_SECTION_REVIEWS == 5
    assert opening_director.MAX_OPENING_VISION_REVIEWS == 4
    assert section_visual_sequence.MAX_SECTION_SEQUENCE_VISION_REVIEWS == 4


def test_no_provider_or_security_owner_is_replaced_by_run169() -> None:
    # Run169 changes retrieval syntax and terminal truth only. The stock provider
    # functions and Security/QR preflight remain owned by the existing runtime stack.
    assert guard.STOCK_CANDIDATE_POOL == 40
    assert visual_selection.MAX_EXTRA_CANDIDATE_INSPECTIONS == 4
    assert callable(orchestrator.pexels_search_videos)
    assert callable(orchestrator.pixabay_provider.search_videos)

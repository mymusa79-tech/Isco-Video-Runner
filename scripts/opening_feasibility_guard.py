from __future__ import annotations

import re
from functools import wraps
from math import ceil

import isco_video_agent.opening_director as opening_director
import isco_video_agent.orchestrator as orchestrator
import isco_video_agent.section_visual_sequence as section_visual_sequence
from isco_video_agent.section_visual_sequence import enforce_section_sequence_duration
from isco_video_agent.security import safe_error
import scripts.planner_quality_guard as planner_quality_guard


OPENING_WINDOW_SECONDS = 30.0
OPENING_COLD_OPEN_SECONDS = 7.0
OPENING_ESCALATION_SECONDS = 11.0
OPENING_PROMISE_SECONDS = 12.0
MAX_TAIL_SHOT_SECONDS = 45.0
MAX_ADAPTIVE_OPENING_SLOTS = 6
MAX_ADAPTIVE_OPENING_REVIEWS = 8
MAX_ADAPTIVE_SECTION_REVIEWS = 5
STOCK_CANDIDATE_POOL = 40

# Keep semantic/environment words that help stock retrieval. Only remove terms that
# force an identifiable staged human subject or add no retrieval value. This fixes
# Run #92's over-collapsed `table room notebook` query without weakening Vision QA.
# Run #169 extends the same search-only transform to temporal/directorial filler that
# describes a mini-scene rather than something a stock index can retrieve reliably.
_SEARCH_DROP_TERMS = set(planner_quality_guard._HUMAN_QUERY_TERMS) | {
    "a",
    "an",
    "the",
    "by",
    "at",
    "in",
    "on",
    "with",
    "near",
    "and",
    "of",
    "from",
    "into",
    "during",
    "sitting",
    "seated",
    "standing",
    "walking",
    "looking",
    "watching",
    "thinking",
    "pensively",
    "reflecting",
    "resting",
    "focused",
    "focus",
    "alone",
    "then",
    "before",
    "after",
    "while",
    "eventually",
    "finally",
    "slowly",
    "suddenly",
    "starting",
    "starts",
    "started",
    "start",
    "beginning",
    "begins",
    "began",
    "pick",
    "picks",
    "picked",
    "picking",
    "up",
    "down",
    "smile",
    "smiles",
    "smiling",
    "contemplative",
    "thoughtful",
    "expression",
    "gaze",
    "turning",
    "turns",
    "moving",
    "moves",
}
_SEARCH_FALLBACK = "quiet room natural light"
_DETAIL_AVOID_TERMS = {
    "room",
    "indoors",
    "indoor",
    "outside",
    "outdoors",
    "outdoor",
    "home",
    "office",
    "light",
    "natural",
    "quiet",
    "calm",
    "background",
    "scene",
}


class VisionVerdictUnavailableError(RuntimeError):
    """No final visual verdict exists because at least one Vision call never completed."""


def stock_safe_search_query(query: str) -> str:
    """Derive a search-only query while preserving the original Vision intent.

    The plan keeps its original visual_query unchanged. Only provider retrieval sees
    this transformed query. Therefore a stock-search safety transform can no longer
    silently redefine what Gemini Vision is asked to judge.
    """
    original = str(query).strip()
    if not original:
        return original

    tokens = re.findall(r"[a-z]+", original.lower())
    if not tokens:
        return original
    if not any(token in planner_quality_guard._HUMAN_QUERY_TERMS for token in tokens):
        return original
    if any(token in planner_quality_guard._SAFE_FRAMING_TERMS for token in tokens):
        return original

    kept = [token for token in tokens if token not in _SEARCH_DROP_TERMS]
    compact = list(dict.fromkeys(kept))
    if len(compact) < 2:
        return _SEARCH_FALLBACK
    return " ".join(compact[:8])


def stock_query_ladder(query: str) -> tuple[str, ...]:
    """Return at most two deterministic retrieval syntaxes for one stable intent.

    This is deliberately *not* another retry loop. The first item is the primary stock
    query already used by Run #92. The optional second item may occupy only the one
    alternate-query slot the Engine already owns. Vision always receives the original
    rich intended_visual through ``_stable_intent_audit``.
    """
    original = str(query).strip()
    primary = stock_safe_search_query(original)
    if not primary:
        return ()

    variants = [primary]
    if primary == original:
        return tuple(variants)

    tokens = re.findall(r"[a-z]+", primary.lower())
    anchors = [token for token in tokens if token not in _DETAIL_AVOID_TERMS]
    if anchors:
        detail = f"{anchors[-1]} closeup"
        if detail != primary:
            variants.append(detail)
    return tuple(variants[:2])


def _bounded_alternate_query_fn(alternate_query_fn, intended_visual: str):
    """Reuse the Engine's single alternate slot; never create a third retrieval phase."""
    ladder = stock_query_ladder(intended_visual)
    deterministic = ladder[1] if len(ladder) > 1 else ""
    primary = ladder[0] if ladder else stock_safe_search_query(intended_visual)

    @wraps(alternate_query_fn)
    def wrapped():
        if deterministic:
            print(f"Visual retrieval ladder alternate query: {deterministic}")
            return deterministic
        proposed = alternate_query_fn()
        normalized = stock_safe_search_query(str(proposed or ""))
        if normalized == primary:
            return ""
        return normalized

    return wrapped


def adaptive_opening_slot_specs(section_seconds: float) -> list[opening_director.OpeningSlotSpec]:
    """Cover the whole first section without demanding one 40-100s stock asset.

    The cinematic opening remains exactly 30 seconds: 7s cold open, 11s escalation,
    12s promise. Any narration after 30s becomes one or more real-stock body slots,
    each no longer than the existing 45s stock-friendly section limit. No asset is
    looped or replayed.
    """
    seconds = enforce_section_sequence_duration(section_seconds)
    if seconds < OPENING_WINDOW_SECONDS:
        return []

    specs = [
        opening_director.OpeningSlotSpec("cold_open", OPENING_COLD_OPEN_SECONDS),
        opening_director.OpeningSlotSpec("escalation", OPENING_ESCALATION_SECONDS),
        opening_director.OpeningSlotSpec("promise", OPENING_PROMISE_SECONDS, primary_section_visual=True),
    ]

    tail = seconds - OPENING_WINDOW_SECONDS
    if tail <= 1e-9:
        return specs

    tail_count = int(ceil(tail / MAX_TAIL_SHOT_SECONDS))
    base = tail / tail_count
    for index in range(tail_count):
        slot_seconds = base if index < tail_count - 1 else tail - base * (tail_count - 1)
        specs.append(opening_director.OpeningSlotSpec(f"body_{index + 1}", slot_seconds))

    if len(specs) > MAX_ADAPTIVE_OPENING_SLOTS:
        raise RuntimeError(
            "adaptive_opening_slot_violation: "
            f"{len(specs)} slots exceeds bounded maximum {MAX_ADAPTIVE_OPENING_SLOTS}"
        )
    return specs


def _adaptive_review_cap(section_seconds: float) -> int:
    specs = adaptive_opening_slot_specs(section_seconds)
    if not specs:
        return opening_director.MAX_OPENING_VISION_REVIEWS
    # Every multi-slot opening gets two bounded semantic-rejection spares. This closes
    # the complete Run #105 failure class rather than only its observed four-slot case:
    # 3..6 required passing assets map to 5..8 reviews. Vision criteria stay unchanged,
    # and the run-wide AI budget remains the final hard ceiling.
    return min(
        MAX_ADAPTIVE_OPENING_REVIEWS,
        max(opening_director.MAX_OPENING_VISION_REVIEWS, len(specs) + 2),
    )


def _adaptive_section_review_cap(section_seconds: float) -> int:
    """Give multi-shot body sections the same bounded rejection headroom as openings.

    Engine's long-section selector can require three distinct passing assets while its
    legacy four-review cap leaves only one semantic rejection spare. That is the exact
    Run #105 failure geometry: enough stock exists, but two honest Vision BLOCKs consume
    the spare reviews before all required slots can be proven. Two-slot sections already
    have two spares at the legacy cap, so only three-slot sections grow from four to five.
    """
    specs = section_visual_sequence.section_slot_specs(section_seconds)
    if not specs:
        return section_visual_sequence.MAX_SECTION_SEQUENCE_VISION_REVIEWS
    return min(
        MAX_ADAPTIVE_SECTION_REVIEWS,
        max(section_visual_sequence.MAX_SECTION_SEQUENCE_VISION_REVIEWS, len(specs) + 2),
    )


_VISION_PROVIDER_FAILURE_ORIGIN = "runner_vision_provider_call_failure"

# Mirrors the transient-provider vocabulary already used by provider_failure.py and
# runtime_reliability.py's own FailurePolicy table - kept as its own bounded list here
# rather than importing either, so a genuine bug/auth/budget exception still propagates
# and crashes loudly instead of being silently treated as "just try the next candidate".
_TRANSIENT_VISION_PROVIDER_MARKERS = (
    "429",
    "quota",
    "rate limit",
    "rate_limit",
    "resource_exhausted",
    "timeout",
    "timed out",
    "connection",
    "network",
    "http 500",
    "http 502",
    "http 503",
    "http 504",
    "server error",
    "service_unavailable",
)


def _is_transient_vision_provider_failure(exc: Exception) -> bool:
    detail = str(exc).lower()
    return any(marker in detail for marker in _TRANSIENT_VISION_PROVIDER_MARKERS)


def _vision_provider_failure_envelope(exc: Exception) -> dict:
    """Transport a failed wire call through the bounded selector without lying about it.

    ``status=block`` is retained only as the Engine selector's legacy transport sentinel:
    review_candidates() understands pass vs non-pass but has no technical-unavailable
    enum. The explicit authority fields below are the source of truth, and the enclosing
    selector raises ``VisionVerdictUnavailableError`` if no later candidate passes.
    The envelope is never a semantic rejection and never weakens any PASS criterion.
    """
    return {
        "status": "block",
        "relevance": 0.0,
        "visual_quality": 0.0,
        "identifiable_person": False,
        "sensitive_trait_implication_risk": False,
        "prominent_logo_or_brand": False,
        "cultural_conflict": False,
        "cultural_islamic_suitability_risk": False,
        "advertiser_conflict": False,
        "obvious_synthetic_or_visual_artifact": False,
        "reason": f"Vision provider call failed technically: {safe_error(exc)}"[:500],
        "review_origin": _VISION_PROVIDER_FAILURE_ORIGIN,
        "vision_review_performed": False,
        "semantic_verdict": False,
        "verdict_authority": "technical_unavailable",
    }


def _stable_intent_audit(audit_fn, intended_visual: str):
    @wraps(audit_fn)
    def wrapped(*args, **kwargs):
        kwargs["intended_visual"] = intended_visual
        try:
            return audit_fn(*args, **kwargs)
        except Exception as exc:
            if not _is_transient_vision_provider_failure(exc):
                raise
            print(
                "Vision provider call failed transiently, skipping this candidate: "
                f"{type(exc).__name__}"
            )
            return _vision_provider_failure_envelope(exc)

    return wrapped


def _technical_unavailable_reviews(result: object) -> list[object]:
    reviews = list(getattr(result, "reviewed", ()) or ())
    return [
        review
        for review in reviews
        if isinstance(getattr(review, "audit", None), dict)
        and getattr(review, "audit").get("review_origin") == _VISION_PROVIDER_FAILURE_ORIGIN
        and getattr(review, "audit").get("vision_review_performed") is False
        and getattr(review, "audit").get("semantic_verdict") is False
    ]


def _enforce_truthful_visual_outcome(result: object, *, scope: str):
    """Do not collapse an unmade Vision judgment into candidate exhaustion.

    A later PASS still wins normally. Only a final failed selector with one or more
    technically unjudged candidates is reclassified. Pure local Security quarantines
    and real semantic BLOCK verdicts retain the Engine's existing failed result, so the
    caller can truthfully report candidate exhaustion.
    """
    if str(getattr(result, "status", "")) != "failed":
        return result

    technical = _technical_unavailable_reviews(result)
    if not technical:
        return result

    reasons = []
    for review in technical:
        audit = getattr(review, "audit", {})
        reason = str(audit.get("reason", "")).strip()
        if reason and reason not in reasons:
            reasons.append(reason)
    detail = " | ".join(reasons[:2]) or "Vision provider call failed technically"
    raise VisionVerdictUnavailableError(
        "VISION_UNAVAILABLE "
        f"scope={scope} technical_candidates={len(technical)} semantic_verdict=false "
        f"reason={detail}"
    )


def _preserve_outline_visual_intent(outline: object, *, fmt: str) -> object:
    """Keep plan.visual_query intact and annotate only the search-only derivative."""
    if fmt not in {"film", "story"} or not isinstance(outline, dict):
        return outline
    briefs = outline.get("section_briefs")
    if not isinstance(briefs, list) or not briefs or not isinstance(briefs[0], dict):
        return outline

    original = str(briefs[0].get("visual_query", "")).strip()
    search_query = stock_safe_search_query(original)
    if search_query and search_query != original:
        briefs[0]["stock_search_query"] = search_query[:260]
        print(
            "Opening feasibility guard separated visual intent from stock query: "
            f"{original} -> {search_query}"
        )
    return outline


def _install_stock_search_wrappers() -> None:
    current_pexels = orchestrator.pexels_search_videos
    if not getattr(current_pexels, "_isco_run92_stock_pool_guard", False):
        @wraps(current_pexels)
        def guarded_pexels(api_key: str, query: str, orientation: str = "landscape", per_page: int = 15):
            effective_query = stock_safe_search_query(query)
            effective_per_page = STOCK_CANDIDATE_POOL if per_page == 12 else per_page
            if effective_query != str(query).strip():
                print(f"Pexels search-only query: {query} -> {effective_query}")
            return current_pexels(
                api_key,
                effective_query,
                orientation=orientation,
                per_page=effective_per_page,
            )

        guarded_pexels._isco_run92_stock_pool_guard = True
        orchestrator.pexels_search_videos = guarded_pexels

    current_pixabay = orchestrator.pixabay_provider.search_videos
    if not getattr(current_pixabay, "_isco_run92_stock_pool_guard", False):
        @wraps(current_pixabay)
        def guarded_pixabay(api_key: str, query: str, orientation: str = "landscape", per_page: int = 15):
            effective_query = stock_safe_search_query(query)
            effective_per_page = STOCK_CANDIDATE_POOL if per_page == 12 else per_page
            if effective_query != str(query).strip():
                print(f"Pixabay search-only query: {query} -> {effective_query}")
            return current_pixabay(
                api_key,
                effective_query,
                orientation=orientation,
                per_page=effective_per_page,
            )

        guarded_pixabay._isco_run92_stock_pool_guard = True
        orchestrator.pixabay_provider.search_videos = guarded_pixabay


def _log_candidate_pool_size(candidates_by_provider: object, *, scope: str) -> None:
    """Observability only - answers a question the current logs cannot: when a section
    ends in "no safe/relevant candidate", was the bounded local-inspection budget
    (max_candidates_per_attempt/max_total_inspections in visual_selection.py) actually
    the limiting factor, or did the stock providers simply return too few raw results
    for this query to matter? Raising that budget without knowing which one is true
    would be a guess, not a fix - this makes the next occurrence diagnosable instead."""
    if not isinstance(candidates_by_provider, dict):
        return
    counts = {
        str(name): len(items) if isinstance(items, list) else 0
        for name, items in candidates_by_provider.items()
    }
    print(f"Visual candidate pool fetched: scope={scope} total={sum(counts.values())} by_provider={counts}")


def _install_selection_wrappers() -> None:
    current_opening_select = opening_director.select_opening_sequence
    if not getattr(current_opening_select, "_isco_run92_adaptive_opening_guard", False):
        @wraps(current_opening_select)
        def guarded_opening_select(*args, **kwargs):
            section_seconds = float(kwargs.get("section_seconds", 0.0) or 0.0)
            intended_visual = str(kwargs.get("intended_visual", ""))
            candidates_by_provider = args[0] if args else kwargs.get("candidates_by_provider")
            _log_candidate_pool_size(candidates_by_provider, scope="opening")
            audit_fn = kwargs.get("audit_fn")
            if callable(audit_fn):
                kwargs["audit_fn"] = _stable_intent_audit(audit_fn, intended_visual)
            alternate_query_fn = kwargs.get("alternate_query_fn")
            if callable(alternate_query_fn):
                kwargs["alternate_query_fn"] = _bounded_alternate_query_fn(
                    alternate_query_fn, intended_visual
                )
            if "max_reviews" not in kwargs:
                kwargs["max_reviews"] = _adaptive_review_cap(section_seconds)
            result = current_opening_select(*args, **kwargs)
            return _enforce_truthful_visual_outcome(result, scope="opening")

        guarded_opening_select._isco_run92_adaptive_opening_guard = True
        guarded_opening_select._isco_run169_visual_truth = True
        opening_director.select_opening_sequence = guarded_opening_select
        orchestrator.select_opening_sequence = guarded_opening_select

    # Alternate queries are retrieval hints, not a replacement editorial intent.
    # Keep the original section intent stable for Vision across the existing normal
    # section recovery paths too. Three-slot long sections also receive the same two
    # bounded semantic-rejection spares that adaptive openings now receive.
    current_section_select = orchestrator.select_section_sequence
    if not getattr(current_section_select, "_isco_run92_stable_visual_intent", False):
        @wraps(current_section_select)
        def guarded_section_select(*args, **kwargs):
            section_seconds = float(kwargs.get("section_seconds", 0.0) or 0.0)
            intended_visual = str(kwargs.get("intended_visual", ""))
            candidates_by_provider = args[0] if args else kwargs.get("candidates_by_provider")
            _log_candidate_pool_size(candidates_by_provider, scope="section")
            audit_fn = kwargs.get("audit_fn")
            if callable(audit_fn):
                kwargs["audit_fn"] = _stable_intent_audit(audit_fn, intended_visual)
            alternate_query_fn = kwargs.get("alternate_query_fn")
            if callable(alternate_query_fn):
                kwargs["alternate_query_fn"] = _bounded_alternate_query_fn(
                    alternate_query_fn, intended_visual
                )
            if "max_reviews" not in kwargs:
                kwargs["max_reviews"] = _adaptive_section_review_cap(section_seconds)
            result = current_section_select(*args, **kwargs)
            return _enforce_truthful_visual_outcome(result, scope="section")

        guarded_section_select._isco_run92_stable_visual_intent = True
        guarded_section_select._isco_run169_visual_truth = True
        orchestrator.select_section_sequence = guarded_section_select

    current_single_select = orchestrator.select_with_recovery
    if not getattr(current_single_select, "_isco_run92_stable_visual_intent", False):
        @wraps(current_single_select)
        def guarded_single_select(*args, **kwargs):
            intended_visual = str(kwargs.get("intended_visual", ""))
            candidates_by_provider = args[0] if args else kwargs.get("candidates_by_provider")
            _log_candidate_pool_size(candidates_by_provider, scope="single")
            audit_fn = kwargs.get("audit_fn")
            if callable(audit_fn):
                kwargs["audit_fn"] = _stable_intent_audit(audit_fn, intended_visual)
            alternate_query_fn = kwargs.get("alternate_query_fn")
            if callable(alternate_query_fn):
                kwargs["alternate_query_fn"] = _bounded_alternate_query_fn(
                    alternate_query_fn, intended_visual
                )
            # PR #506's diagnostic proved this real gap: a real run fetched 80 raw
            # candidates (Visual candidate pool fetched: total=80) but only 7 ever got a
            # local preflight look before the section failed as "no safe/relevant
            # candidate". visual_selection.select_with_recovery()'s own default caps the
            # *primary* attempt's local-inspection ceiling at
            # max_candidates_per_attempt + 4 regardless of max_total_inspections, but the
            # *alternate* attempt's ceiling is max_total_inspections minus whatever the
            # primary attempt already inspected - uncapped by that same formula. Raising
            # only max_total_inspections therefore gives the alternate-query recovery
            # phase far more of an already-fetched, already-paid-for-in-bandwidth pool to
            # locally check for free, with zero change to max_candidates_per_attempt -
            # the real, paid Vision-call ceiling for this path stays exactly what it is
            # today, consistent with this codebase's own conservative convention for that
            # number (see short_cinematic_director.py's even smaller value).
            if "max_total_inspections" not in kwargs:
                kwargs["max_total_inspections"] = STOCK_CANDIDATE_POOL
            result = current_single_select(*args, **kwargs)
            return _enforce_truthful_visual_outcome(result, scope="single")

        guarded_single_select._isco_run92_stable_visual_intent = True
        guarded_single_select._isco_run169_visual_truth = True
        orchestrator.select_with_recovery = guarded_single_select


def install_opening_feasibility_guard() -> None:
    """Install shared bounded stock retrieval + truthful Vision outcome ownership."""
    # planner_quality_guard's installed outline wrapper resolves this module global at
    # call time, so replacing it here stops the old destructive visual_query rewrite
    # while preserving the rest of that guard unchanged.
    planner_quality_guard._guard_opening_brief = _preserve_outline_visual_intent

    # The Engine selector looks up opening_slot_specs from its module globals at call
    # time. Replacing only that geometry keeps the existing distinct-asset, exclusion,
    # alternate-search and Vision verdict logic intact.
    opening_director.opening_slot_specs = adaptive_opening_slot_specs

    _install_stock_search_wrappers()
    _install_selection_wrappers()

    print(
        "Opening feasibility guard installed: fixed 30s opening; adaptive <=45s tail; "
        "search/intent separation; bounded retrieval ladder; truthful Vision-unavailable; "
        "40-result stock pool; existing Vision QA caps preserved"
    )

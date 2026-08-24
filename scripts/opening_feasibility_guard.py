from __future__ import annotations

import re
from functools import wraps
from math import ceil

import isco_video_agent.opening_director as opening_director
import isco_video_agent.orchestrator as orchestrator
import isco_video_agent.section_visual_sequence as section_visual_sequence
from isco_video_agent.section_visual_sequence import enforce_section_sequence_duration
import scripts.planner_quality_guard as planner_quality_guard


OPENING_WINDOW_SECONDS = 30.0
OPENING_COLD_OPEN_SECONDS = 7.0
OPENING_ESCALATION_SECONDS = 11.0
OPENING_PROMISE_SECONDS = 12.0
MAX_TAIL_SHOT_SECONDS = 45.0
MAX_ADAPTIVE_OPENING_SLOTS = 6
MAX_ADAPTIVE_OPENING_REVIEWS = 7
MAX_ADAPTIVE_SECTION_REVIEWS = 5
STOCK_CANDIDATE_POOL = 40

# Keep semantic/environment words that help stock retrieval. Only remove terms that
# force an identifiable staged human subject or add no retrieval value. This fixes
# Run #92's over-collapsed `table room notebook` query without weakening Vision QA.
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
}
_SEARCH_FALLBACK = "quiet room natural light"


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
    # Keep the legacy four-review contract for the normal three-shot opening. When
    # the first section creates extra tail slots, reserve two semantic-rejection
    # reviews (bounded oversampling) instead of only one. This mirrors established
    # retrieve -> prefilter -> rerank pipelines and fixes Run #105's 3-pass/2-block
    # dead end without weakening Vision QA or increasing the hard cap above seven.
    rejection_headroom = 1 if len(specs) <= 3 else 2
    return min(
        MAX_ADAPTIVE_OPENING_REVIEWS,
        max(opening_director.MAX_OPENING_VISION_REVIEWS, len(specs) + rejection_headroom),
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


def _stable_intent_audit(audit_fn, intended_visual: str):
    @wraps(audit_fn)
    def wrapped(*args, **kwargs):
        kwargs["intended_visual"] = intended_visual
        return audit_fn(*args, **kwargs)

    return wrapped


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


def _install_selection_wrappers() -> None:
    current_opening_select = opening_director.select_opening_sequence
    if not getattr(current_opening_select, "_isco_run92_adaptive_opening_guard", False):
        @wraps(current_opening_select)
        def guarded_opening_select(*args, **kwargs):
            section_seconds = float(kwargs.get("section_seconds", 0.0) or 0.0)
            intended_visual = str(kwargs.get("intended_visual", ""))
            audit_fn = kwargs.get("audit_fn")
            if callable(audit_fn):
                kwargs["audit_fn"] = _stable_intent_audit(audit_fn, intended_visual)
            if "max_reviews" not in kwargs:
                kwargs["max_reviews"] = _adaptive_review_cap(section_seconds)
            return current_opening_select(*args, **kwargs)

        guarded_opening_select._isco_run92_adaptive_opening_guard = True
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
            audit_fn = kwargs.get("audit_fn")
            if callable(audit_fn):
                kwargs["audit_fn"] = _stable_intent_audit(audit_fn, intended_visual)
            if "max_reviews" not in kwargs:
                kwargs["max_reviews"] = _adaptive_section_review_cap(section_seconds)
            return current_section_select(*args, **kwargs)

        guarded_section_select._isco_run92_stable_visual_intent = True
        orchestrator.select_section_sequence = guarded_section_select

    current_single_select = orchestrator.select_with_recovery
    if not getattr(current_single_select, "_isco_run92_stable_visual_intent", False):
        @wraps(current_single_select)
        def guarded_single_select(*args, **kwargs):
            intended_visual = str(kwargs.get("intended_visual", ""))
            audit_fn = kwargs.get("audit_fn")
            if callable(audit_fn):
                kwargs["audit_fn"] = _stable_intent_audit(audit_fn, intended_visual)
            return current_single_select(*args, **kwargs)

        guarded_single_select._isco_run92_stable_visual_intent = True
        orchestrator.select_with_recovery = guarded_single_select


def install_opening_feasibility_guard() -> None:
    """Install the Run #92 retrieval/duration fix without weakening any QA gate."""
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
        "search/intent separation; 40-result stock pool; bounded Vision QA unchanged"
    )

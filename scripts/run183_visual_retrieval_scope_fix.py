from __future__ import annotations

"""Runtime/lifecycle hardening for Run183 Visual Retrieval closure.

The first Run183 implementation correctly added semantic query families and cross-pool
candidate dedup, but full regression exposed two ownership details that must stay true:

1. Historical Run169 query-ladder behavior is a diagnostic/test contract outside live
   Production; Run183 semantics must activate only inside canonical orchestrator.produce.
2. "Already reviewed in this selector" must include cache hits, not only candidates that
   happened to create a new cache entry.

Run185 exposed a third composition boundary: Opening Feasibility's historical
``_stable_intent_audit`` correctly kept search-only syntax changes from redefining Vision
truth, but it also erased Run183's deliberately semantic alternate intent. That made a
candidate retrieved for e.g. ``personal boundaries calm conversation`` get judged against
the old literal marker/line staging instead. This layer now allows only the exact bounded
Run183 semantic alternates for the current production selector to reach Vision; all other
queries still collapse back to the original rich intent.

This layer preserves those contracts while keeping the production improvements active.
It also sharpens the Run183 boundary+relationship intent so a high provider-rank
whiteboard/marker result cannot outrank a lower provider-rank candidate whose metadata
actually matches personal-boundary/relationship/conversation semantics.
"""

from contextvars import ContextVar
from functools import wraps

import isco_video_agent.opening_director as opening_director
import isco_video_agent.orchestrator as orchestrator
import isco_video_agent.section_visual_sequence as section_visual_sequence
import isco_video_agent.visual_selection as visual_selection
from scripts import opening_feasibility_guard as opening_guard
from scripts import run183_visual_retrieval_closure as run183
from scripts import visual_retrieval_adjudication_v1 as v1


_INSTALLED = False
_REVIEWED_CURRENT_SELECTOR: ContextVar[set[tuple[str, object]] | None] = ContextVar(
    "isco_run183_reviewed_current_selector",
    default=None,
)
_TRUSTED_SEMANTIC_INTENTS: ContextVar[frozenset[str] | None] = ContextVar(
    "isco_run185_trusted_semantic_visual_intents",
    default=None,
)


def _runtime_active() -> bool:
    # Lazy import avoids a construction-time cycle: runtime_scope imports V1 while this
    # module is installed immediately before runtime_scope itself.
    try:
        from scripts.visual_retrieval_runtime_scope_v1 import active

        return bool(active())
    except Exception:
        return False


def _scoped_query_ladder(legacy_ladder, semantic_ladder):
    @wraps(legacy_ladder)
    def wrapped(query: str):
        if _runtime_active():
            return semantic_ladder(query)
        return legacy_ladder(query)

    wrapped._isco_run183_query_ladder_scope = True
    wrapped._isco_run183_legacy = legacy_ladder
    wrapped._isco_run183_active = semantic_ladder
    return wrapped


def _scoped_build_visual_intent(legacy_builder):
    @wraps(legacy_builder)
    def wrapped(text: object):
        if _runtime_active():
            return run183._enriched_build_visual_intent(text)
        return legacy_builder(text)

    wrapped._isco_run183_intent_scope = True
    wrapped._isco_run183_legacy = legacy_builder
    return wrapped


def _refine_run183_intent(original):
    """Prefer abstract editorial anchors over literal props in the Run183 geometry."""
    @wraps(original)
    def wrapped(text: object) -> v1.VisualIntent:
        base = original(text)
        labels = run183._concept_labels(text)
        if not {"boundary", "relationship"}.issubset(labels):
            return base

        # Replace, rather than merely append to, the literal anchor set. The literal
        # words line/marker/drawing are how the stock provider found the whiteboard clip;
        # once the relationship-boundary interpretation is certain, retaining them as
        # semantic anchors lets provider rank prior overpower the actual editorial fit.
        anchors = frozenset({"boundary", "relationship", "conversation", "space"})
        expanded = frozenset({
            "boundary", "limit", "space", "distance", "separate", "separation",
            "relationship", "conversation", "talk", "partner", "together", "personal",
            "interaction", "calm", "resolved", "clarity",
        })
        return v1.VisualIntent(raw=base.raw, anchors=anchors, expanded=expanded)

    wrapped._isco_run183_refined_intent = True
    wrapped._isco_run183_original = original
    return wrapped


def _trusted_semantic_intents(intended_visual: object, narration_context: object) -> frozenset[str]:
    """Return only semantic alternates owned by the bounded Run183 family for this selector."""
    family = run183.semantic_query_family(intended_visual, narration_context)
    trusted: set[str] = set()
    for query in family.alternates:
        normalized = run183._safe_query(query)
        if normalized:
            trusted.add(normalized)
    return frozenset(trusted)


def _semantic_recovery_stable_intent(original):
    """Keep Run92/169 stable-intent truth except for proven Run183 semantic recovery.

    Opening Feasibility intentionally forces provider-search syntaxes back to the original
    editorial visual before Vision. Run183 is different: its alternate is a bounded
    semantic recovery concept, not merely another stock-search spelling. The selector
    scope below records the exact permitted alternates. Only those exact normalized
    values, and only inside canonical Production, may bypass the historical rewrite.

    The transient-provider handling is kept byte-for-byte equivalent in behavior by
    reusing Opening Feasibility's classifier and failure envelope. Security, relevance,
    quality thresholds and the four-review ceiling remain untouched.
    """
    if getattr(original, "_isco_run185_semantic_visual_truth", False):
        return original

    @wraps(original)
    def builder(audit_fn, intended_visual: str):
        stable = original(audit_fn, intended_visual)

        @wraps(stable)
        def semantic_aware(*args, **kwargs):
            proposed = str(kwargs.get("intended_visual") or "").strip()
            normalized = run183._safe_query(proposed)
            trusted = _TRUSTED_SEMANTIC_INTENTS.get() or frozenset()
            if _runtime_active() and normalized and normalized in trusted:
                kwargs["intended_visual"] = normalized
                try:
                    return audit_fn(*args, **kwargs)
                except Exception as exc:
                    if not opening_guard._is_transient_vision_provider_failure(exc):
                        raise
                    print(
                        "Vision provider call failed transiently, skipping this candidate: "
                        f"{type(exc).__name__}"
                    )
                    return opening_guard._vision_provider_failure_envelope(exc)
            return stable(*args, **kwargs)

        semantic_aware._isco_run185_semantic_visual_truth = True
        semantic_aware._isco_run185_stable_fallback = stable
        return semantic_aware

    builder._isco_run185_semantic_visual_truth = True
    builder._isco_run185_original = original
    return builder


def _record_reviews(result: object) -> None:
    reviewed = _REVIEWED_CURRENT_SELECTOR.get()
    if reviewed is None:
        return
    for review in list(getattr(result, "reviewed", ()) or ()):
        provider = str(getattr(review, "provider", "") or "").strip().lower()
        candidate = getattr(review, "candidate", None)
        if not provider or not isinstance(candidate, dict):
            continue
        reviewed.add(run183._candidate_identity(provider, candidate))


def _review_recorder(current):
    if getattr(current, "_isco_run183_review_recorder", False):
        return current

    @wraps(current)
    def wrapped(*args, **kwargs):
        result = current(*args, **kwargs)
        _record_reviews(result)
        return result

    wrapped._isco_run183_review_recorder = True
    wrapped._isco_run183_original = current
    return wrapped


def _selector_review_scope(current):
    if getattr(current, "_isco_run183_review_scope", False):
        return current

    @wraps(current)
    def wrapped(*args, **kwargs):
        existing = _REVIEWED_CURRENT_SELECTOR.get()
        if existing is not None:
            return current(*args, **kwargs)

        intended_visual = str(kwargs.get("intended_visual") or "").strip()
        narration_context = str(kwargs.get("narration_context") or "").strip()
        trusted = _trusted_semantic_intents(intended_visual, narration_context)
        review_token = _REVIEWED_CURRENT_SELECTOR.set(set())
        intent_token = _TRUSTED_SEMANTIC_INTENTS.set(trusted)
        try:
            return current(*args, **kwargs)
        finally:
            _TRUSTED_SEMANTIC_INTENTS.reset(intent_token)
            _REVIEWED_CURRENT_SELECTOR.reset(review_token)

    wrapped._isco_run183_review_scope = True
    # Runtime Scope V1 resolves this historical seam when Production is inactive.
    wrapped._isco_visual_intent_original = getattr(current, "_isco_visual_intent_original", current)
    wrapped._isco_run183_original = current
    return wrapped


def _reviewed_pairs_with_cache_fallback(original):
    @wraps(original)
    def wrapped(cache: object, before: set[tuple[object, ...]]) -> set[tuple[str, object]]:
        pairs = set(original(cache, before))
        current = _REVIEWED_CURRENT_SELECTOR.get()
        if current:
            pairs.update(current)
        return pairs

    wrapped._isco_run183_review_registry = True
    wrapped._isco_run183_original = original
    return wrapped


def _install_review_recording() -> None:
    current = visual_selection.review_candidates
    recorded = _review_recorder(current)
    visual_selection.review_candidates = recorded

    # Opening/section modules imported the callable directly, so update only if they
    # still point at the same pre-wrapper function. This changes no verdict semantics;
    # it records the CandidateReview objects the existing selector already produced.
    if opening_director.review_candidates is current:
        opening_director.review_candidates = recorded
    if section_visual_sequence.review_candidates is current:
        section_visual_sequence.review_candidates = recorded

    current_opening = opening_director.select_opening_sequence
    scoped_opening = _selector_review_scope(current_opening)
    opening_director.select_opening_sequence = scoped_opening
    if orchestrator.select_opening_sequence is current_opening:
        orchestrator.select_opening_sequence = scoped_opening

    current_section = section_visual_sequence.select_section_sequence
    scoped_section = _selector_review_scope(current_section)
    section_visual_sequence.select_section_sequence = scoped_section
    if orchestrator.select_section_sequence is current_section:
        orchestrator.select_section_sequence = scoped_section

    current_single = visual_selection.select_with_recovery
    scoped_single = _selector_review_scope(current_single)
    visual_selection.select_with_recovery = scoped_single
    if orchestrator.select_with_recovery is current_single:
        orchestrator.select_with_recovery = scoped_single

    run183._reviewed_pairs_since = _reviewed_pairs_with_cache_fallback(run183._reviewed_pairs_since)


def install_run183_visual_retrieval_scope_fix(
    *,
    legacy_query_ladder,
    legacy_build_visual_intent,
) -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    # Refine the production-only semantic interpretation first. All Run183 helpers read
    # this module global dynamically, so query family and local reranking use one meaning.
    run183._enriched_build_visual_intent = _refine_run183_intent(
        run183._enriched_build_visual_intent
    )

    # Restore historical observable behavior outside Production while keeping the new
    # semantics in the canonical run. This specifically preserves Run169's regression
    # assertions and prevents process-wide policy contamination.
    opening_guard.stock_query_ladder = _scoped_query_ladder(
        legacy_query_ladder,
        run183.semantic_stock_query_ladder,
    )
    v1.build_visual_intent = _scoped_build_visual_intent(legacy_build_visual_intent)

    # Run185: compose semantic recovery with the historical stable-intent boundary
    # before Opening Feasibility installs its selector wrappers later in run_v3_voice.
    # The wrapper still defaults to the original intent; only exact current-selector
    # Run183 alternates can reach Vision during canonical Production.
    opening_guard._stable_intent_audit = _semantic_recovery_stable_intent(
        opening_guard._stable_intent_audit
    )

    _install_review_recording()
    _INSTALLED = True
    print(
        "Run183 Visual Retrieval scope fix installed: production-only semantic ladder/intent; "
        "current-selector review registry includes cache hits; Run185 semantic alternate Vision truth; "
        "historical Run169/V1 diagnostics preserved"
    )

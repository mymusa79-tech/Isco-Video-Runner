from __future__ import annotations

"""Run #212/#213 visual candidate-utilization closure.

Run212 proved that broad stock recall, semantic recovery fusion, local MMR-style
reranking and the cloud Vision authority can all work while a Short still fails: the
provider-search transform can discard a late beat modifier, a severe semantic BLOCK can
be paid for again under a new beat context, and Short Cinematic can expose too little of
the fused pool per attempt.

Run213 then proved the narrower precision gap after those fixes were live: the bounded
four-review Short window worked, but style/framing words still consumed the eight-token
provider query and the shared semantic recovery family could prefer a topic-level concept
(``focus``) over the current beat concept (``contrasting perspective``).

This module closes those gaps without weakening any Visual/Security/Cultural threshold
and without adding an unbounded retry/query loop:

* Long + Short retrieval query compaction removes provider-irrelevant style/framing noise
  and keeps both the stable scene concept and the late corrective/beat concept.
* Short-only request scope prioritizes deterministic, safe-framed beat-specific semantic
  recovery queries before the existing shared topic fallback; Run183 fanout remains two.
* Short-only request scope quarantines severe semantic hard negatives across later beats.
* Short-only request scope exposes at most two primary and two fused-recovery Vision
  verdicts. PASS still stops immediately, so easy beats do not pay the full ceiling.

The Long Opening/Section selectors already own adaptive rejection headroom (up to 8/5)
and already exclude every reviewed asset between sequence slots, so those budgets remain
unchanged. Long receives only the shared query-precision improvement.
"""

import math
import re
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Iterator

from scripts import opening_feasibility_guard as opening_guard
from scripts import run183_visual_retrieval_closure as run183
from scripts import run200_short_vision_recovery_closure as run200
from scripts import short_cinematic_director as short_director


CONTRACT_ID = "run212-visual-candidate-utilization-v2"
CONTRACT_VERSION = 2
SHORT_VISION_REVIEWS_PER_ATTEMPT = 2
SHORT_VISION_REVIEWS_PER_BEAT = 4
SHORT_TOTAL_INSPECTIONS_PER_BEAT = 8
HARD_NEGATIVE_RELEVANCE_MAX = 0.25

# These words describe render/framing style that the provider API already receives via
# orientation or that Vision can judge later. Keeping them inside an eight-token stock
# query crowds out the semantic action/concept that should drive retrieval. Run213 added
# the remaining aesthetic terms that consumed the live beat-2 query. "professional" is
# deliberately retained because, once direct human nouns are safety-compacted away, it
# remains useful business/work context and prevents generic beauty-portrait drift.
_SEARCH_NOISE_TERMS = {
    "cinematic",
    "shot",
    "portrait",
    "vertical",
    "realistic",
    "grounded",
    "aesthetic",
    "subtle",
    "medium",
    "close",
    "closeup",
    "lighting",
    "atmospheric",
    "documentary",
    "style",
    "minimalist",
    "sunlit",
    "morning",
    "calm",
    "their",
    "his",
    "her",
}

# Stock-friendly recovery phrases for the finite Short template catalog. Safe-framing
# anchors (hands/back/silhouette/shadow) keep human action retrievable without forcing an
# identifiable staged face. These are retrieval hints only; Vision remains final semantic
# authority and every existing cultural/security gate remains unchanged.
_SHORT_BEAT_RECOVERY: dict[str, tuple[str, str]] = {
    "immediate visual tension": (
        "hands tense desk choices",
        "hands overwhelmed paperwork desk",
    ),
    "contrasting perspective realistic": (
        "hands choosing options desk",
        "hands decision notebook desk",
    ),
    "person changing direction subtle action": (
        "back walking crossroads direction",
        "silhouette choosing path crossroads",
    ),
    "hopeful practical movement": (
        "back walking forward sunlight",
        "hands completing task desk",
    ),
    "intimate reflective close detail": (
        "hands face reflection quiet",
        "silhouette reflecting window",
    ),
    "hesitation hands subtle tension": (
        "hands hesitation desk choice",
        "hands pause decision notebook",
    ),
    "perspective shift realistic human action": (
        "hands rearranging notes desk",
        "back changing direction",
    ),
    "person standing moving forward hopeful": (
        "back walking forward hopeful",
        "silhouette moving sunlight",
    ),
    "immediate establishing action": (
        "hands starting task desk",
        "hands opening notebook desk",
    ),
    "concrete human action detail": (
        "hands working notebook desk",
        "hands completing task desk",
    ),
    "clear turning point realistic": (
        "hands choosing option desk",
        "back changing direction",
    ),
    "forward movement meaningful payoff": (
        "hands finishing task desk",
        "back walking forward progress",
    ),
    "calm symbolic visual detail": (
        "hands calm object desk",
        "shadow reflective window",
    ),
    "quiet reflective pause": (
        "hands resting desk quiet",
        "silhouette quiet window",
    ),
    "subtle perspective shift": (
        "hands rearranging notes desk",
        "shadow changing perspective",
    ),
    "gentle hopeful release": (
        "back walking sunlight hopeful",
        "hands relaxed desk",
    ),
}

_SHARED_INSTALLED = False


def _balanced_stock_query(original: object) -> str:
    """Mirror Run92 safety semantics while retaining semantic head and tail.

    The historical transform used ``compact[:8]``. Run212 moved Short beat modifiers to
    the front and used head4+tail4, but Run213 showed that style words such as
    ``atmospheric lighting documentary style`` could still occupy the tail. We keep the
    same human/safe-framing admission rule and eight-token ceiling while removing those
    non-retrieval terms first. Vision still receives the original rich intended_visual.
    """
    raw = str(original or "").strip()
    if not raw:
        return raw

    tokens = re.findall(r"[a-z]+", raw.lower())
    if not tokens:
        return raw
    if not any(token in opening_guard.planner_quality_guard._HUMAN_QUERY_TERMS for token in tokens):
        return raw
    if any(token in opening_guard.planner_quality_guard._SAFE_FRAMING_TERMS for token in tokens):
        return raw

    dropped = set(opening_guard._SEARCH_DROP_TERMS) | _SEARCH_NOISE_TERMS
    kept = [token for token in tokens if token not in dropped]
    compact = list(dict.fromkeys(kept))
    if len(compact) < 2:
        return opening_guard._SEARCH_FALLBACK
    if len(compact) <= 8:
        return " ".join(compact)

    balanced = list(dict.fromkeys([*compact[:4], *compact[-4:]]))
    return " ".join(balanced[:8])


def install_shared_visual_candidate_utilization() -> None:
    """Install semantic-noise-aware stock-query compaction for Long + Short runtime."""
    global _SHARED_INSTALLED
    current = opening_guard.stock_safe_search_query
    if getattr(current, "_isco_run212_balanced_stock_query", False):
        _SHARED_INSTALLED = True
        return

    @wraps(current)
    def wrapped(query: str) -> str:
        return _balanced_stock_query(query)

    wrapped._isco_run212_balanced_stock_query = True
    wrapped._isco_run212_original = current
    opening_guard.stock_safe_search_query = wrapped
    _SHARED_INSTALLED = True
    print(
        "Run212 Visual Candidate Utilization installed: semantic head+tail stock query; "
        "Long+Short Vision/Security/Cultural verdict thresholds unchanged"
    )


def _asset_pair(provider: object, asset_id: object) -> tuple[str, str]:
    return str(provider or "").strip().lower(), str(asset_id or "").strip()


def _severe_semantic_hard_negative(result: object) -> bool:
    if not isinstance(result, dict) or str(result.get("status") or "").strip().lower() != "block":
        return False
    if result.get("vision_review_performed") is not True:
        return False
    if result.get("semantic_verdict") is False:
        return False
    if str(result.get("verdict_authority") or "").strip().lower() == "technical_unavailable":
        return False
    try:
        relevance = float(result.get("relevance"))
    except (TypeError, ValueError):
        return False
    return math.isfinite(relevance) and relevance <= HARD_NEGATIVE_RELEVANCE_MAX


def _hard_negative_cache_type(base_type):
    """Extend the active cache type without changing its context-aware verdict store."""

    class Run212HardNegativeCache(base_type):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._run212_hard_negatives: set[tuple[str, str]] = set()

        def set(self, provider, asset_id, ctx_hash, result):
            super().set(provider, asset_id, ctx_hash, result)
            if _severe_semantic_hard_negative(result):
                self._run212_hard_negatives.add(_asset_pair(provider, asset_id))

        def unavailable(self, provider, asset_id):
            if _asset_pair(provider, asset_id) in self._run212_hard_negatives:
                return True
            return super().unavailable(provider, asset_id)

    Run212HardNegativeCache.__name__ = f"Run212{getattr(base_type, '__name__', 'VisualCandidateCache')}"
    Run212HardNegativeCache._isco_run212_hard_negative_cache = True
    return Run212HardNegativeCache


def _beat_queries_with_priority_tail_preservation(
    base_query: str,
    template: str,
    index: int,
) -> tuple[str, str]:
    """Put the beat-specific concept before the base so retrieval cannot erase it."""
    if template not in short_director._TEMPLATE_QUERY_MODIFIERS:
        raise short_director.ShortCinematicError(
            "Short cinematic director received unsupported template"
        )
    modifiers = short_director._TEMPLATE_QUERY_MODIFIERS[template]
    alternates = short_director._TEMPLATE_ALT_MODIFIERS[template]
    slot = min(max(0, int(index)), len(modifiers) - 1)
    base = short_director._clean(base_query, 200)
    if not base:
        raise short_director.ShortCinematicError(
            "Short cinematic director requires an English visual query"
        )
    return (
        short_director._clean(
            f"{modifiers[slot]} {base} portrait vertical realistic cinematic", 260
        ),
        short_director._clean(
            f"{alternates[slot]} {base} portrait vertical realistic cinematic", 260
        ),
    )


def _detect_short_beat_modifier(intended_visual: object) -> str:
    raw = " ".join(str(intended_visual or "").strip().casefold().split())
    for modifiers in short_director._TEMPLATE_QUERY_MODIFIERS.values():
        for modifier in modifiers:
            normalized = " ".join(modifier.casefold().split())
            if raw.startswith(normalized):
                return modifier
    return ""


def _short_semantic_query_family(
    original_family,
    intended_visual: object,
    narration_context: object = "",
):
    """Prefer the active Short beat's concrete recovery semantics before topic fallback."""
    family = original_family(intended_visual, narration_context)
    modifier = _detect_short_beat_modifier(intended_visual)
    beat_queries = _SHORT_BEAT_RECOVERY.get(modifier, ())
    if not beat_queries:
        return family

    variants: list[str] = []
    for query in (*beat_queries, *family.alternates):
        normalized = _balanced_stock_query(query).strip()
        if normalized and normalized != family.primary and normalized not in variants:
            variants.append(normalized)
        if len(variants) >= run183.MAX_ALTERNATE_QUERY_FANOUT:
            break

    labels = set(family.labels)
    labels.add("short_beat")
    print(
        "Run213 Short beat semantic recovery: "
        f"modifier={modifier} alternates={variants} topic_fallback_deferred=true"
    )
    return run183.SemanticRetrievalFamily(
        primary=family.primary,
        alternates=tuple(variants),
        labels=frozenset(labels),
    )


@contextmanager
def short_candidate_utilization_scope(root: Path) -> Iterator[None]:
    """Compose Run212/213 only around one authoritative Short finishing request."""
    original_beat_queries = short_director.beat_queries
    original_cache = short_director.VisualCandidateCache
    original_per_attempt = short_director.MAX_VISION_REVIEWS_PER_ATTEMPT
    original_per_beat = short_director.MAX_VISION_REVIEWS_PER_BEAT
    original_inspections = short_director.MAX_TOTAL_INSPECTIONS_PER_BEAT
    original_semantic_family = run183.semantic_query_family

    def short_semantic_family(intended_visual, narration_context=""):
        return _short_semantic_query_family(
            original_semantic_family,
            intended_visual,
            narration_context,
        )

    short_director.beat_queries = _beat_queries_with_priority_tail_preservation
    short_director.VisualCandidateCache = _hard_negative_cache_type(original_cache)
    short_director.MAX_VISION_REVIEWS_PER_ATTEMPT = SHORT_VISION_REVIEWS_PER_ATTEMPT
    short_director.MAX_VISION_REVIEWS_PER_BEAT = SHORT_VISION_REVIEWS_PER_BEAT
    short_director.MAX_TOTAL_INSPECTIONS_PER_BEAT = SHORT_TOTAL_INSPECTIONS_PER_BEAT
    run183.semantic_query_family = short_semantic_family

    try:
        with run200.short_vision_recovery_scope(Path(root)):
            yield
    finally:
        run183.semantic_query_family = original_semantic_family
        short_director.beat_queries = original_beat_queries
        short_director.VisualCandidateCache = original_cache
        short_director.MAX_VISION_REVIEWS_PER_ATTEMPT = original_per_attempt
        short_director.MAX_VISION_REVIEWS_PER_BEAT = original_per_beat
        short_director.MAX_TOTAL_INSPECTIONS_PER_BEAT = original_inspections

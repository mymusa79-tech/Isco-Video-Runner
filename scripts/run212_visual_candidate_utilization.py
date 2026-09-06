from __future__ import annotations

"""Run #212 visual candidate-utilization closure.

Run212 proved that broad stock recall, semantic recovery fusion, local MMR-style
reranking and the cloud Vision authority can all work while a Short still fails: the
provider-search transform can discard a late beat modifier, a severe semantic BLOCK can
be paid for again under a new beat context, and Short Cinematic exposes only one
semantic candidate per primary/recovery attempt.

This module closes those remaining gaps without weakening any Visual/Security/Cultural
threshold and without adding an unbounded retry/query loop:

* Long + Short retrieval query compaction keeps both the stable head concept and the
  late corrective/beat concept instead of blindly keeping the first eight tokens.
* Short-only request scope quarantines severe semantic hard negatives across later beats.
* Short-only request scope raises the per-attempt review window from 1 to 2. The shared
  selector remains PASS=STOP, so easy beats still cost one Vision verdict; only honest
  BLOCKs unlock the second candidate and the one existing recovery phase. Absolute Short
  ceiling becomes four semantic verdicts per added beat (2 primary + 2 fused recovery).

The Long Opening/Section selectors already own adaptive rejection headroom (up to 8/5)
and already exclude every reviewed asset between sequence slots, so those two policies
remain unchanged.
"""

import math
import re
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Iterator

from scripts import opening_feasibility_guard as opening_guard
from scripts import run200_short_vision_recovery_closure as run200
from scripts import short_cinematic_director as short_director


CONTRACT_ID = "run212-visual-candidate-utilization-v1"
CONTRACT_VERSION = 1
SHORT_VISION_REVIEWS_PER_ATTEMPT = 2
SHORT_VISION_REVIEWS_PER_BEAT = 4
SHORT_TOTAL_INSPECTIONS_PER_BEAT = 8
HARD_NEGATIVE_RELEVANCE_MAX = 0.25

# These words describe render/framing style that the provider API already receives via
# orientation or that Vision can judge later. Keeping them inside an eight-token stock
# query crowds out the semantic beat modifier that should drive retrieval.
_SEARCH_NOISE_TERMS = {
    "cinematic",
    "shot",
    "portrait",
    "vertical",
    "realistic",
    "professional",
    "grounded",
    "aesthetic",
    "subtle",
    "their",
    "his",
    "her",
}

_SHARED_INSTALLED = False


def _balanced_stock_query(original: object) -> str:
    """Mirror Run92 safety semantics while retaining both query head and tail.

    The historical transform used ``compact[:8]``. Short beat modifiers are appended to
    the base query, so Run212's decisive ``changing direction ... action`` tail vanished
    before Pexels/Pixabay saw it. We keep the same human/safe-framing admission rule and
    the same eight-token ceiling, but remove pure style noise and use head4 + tail4 when
    compaction is necessary. Vision still receives the original rich intended_visual.
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
    """Install tail-aware stock-query compaction for canonical Long + Short runtime."""
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
        "Run212 Visual Candidate Utilization installed: balanced head+tail stock query; "
        "Long+Short Vision/Security/Cultural verdict thresholds unchanged"
    )


def _asset_pair(provider: object, asset_id: object) -> tuple[str, str]:
    return str(provider or "").strip().lower(), str(asset_id or "").strip()


def _severe_semantic_hard_negative(result: object) -> bool:
    if not isinstance(result, dict) or str(result.get("status") or "").strip().lower() != "block":
        return False
    # Technical unavailability is not evidence about the candidate and must remain
    # eligible for the existing bounded half-open/provider-recovery policy.
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


@contextmanager
def short_candidate_utilization_scope(root: Path) -> Iterator[None]:
    """Compose Run212 only around one authoritative Short finishing request."""
    original_beat_queries = short_director.beat_queries
    original_cache = short_director.VisualCandidateCache
    original_per_attempt = short_director.MAX_VISION_REVIEWS_PER_ATTEMPT
    original_per_beat = short_director.MAX_VISION_REVIEWS_PER_BEAT
    original_inspections = short_director.MAX_TOTAL_INSPECTIONS_PER_BEAT

    short_director.beat_queries = _beat_queries_with_priority_tail_preservation
    short_director.VisualCandidateCache = _hard_negative_cache_type(original_cache)
    short_director.MAX_VISION_REVIEWS_PER_ATTEMPT = SHORT_VISION_REVIEWS_PER_ATTEMPT
    short_director.MAX_VISION_REVIEWS_PER_BEAT = SHORT_VISION_REVIEWS_PER_BEAT
    short_director.MAX_TOTAL_INSPECTIONS_PER_BEAT = SHORT_TOTAL_INSPECTIONS_PER_BEAT

    try:
        with run200.short_vision_recovery_scope(Path(root)):
            yield
    finally:
        short_director.beat_queries = original_beat_queries
        short_director.VisualCandidateCache = original_cache
        short_director.MAX_VISION_REVIEWS_PER_ATTEMPT = original_per_attempt
        short_director.MAX_VISION_REVIEWS_PER_BEAT = original_per_beat
        short_director.MAX_TOTAL_INSPECTIONS_PER_BEAT = original_inspections

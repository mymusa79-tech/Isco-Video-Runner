from __future__ import annotations

"""Run215 weighted-RRF + Vision-feedback extension of visual family F29.

Run215 proved that Run214's canonical-intent boundary and global rerank were active and
that the core Moment visual could pass, yet Short Cinematic beat 3 still failed after a
large recovery fusion. The recovery pool contained a materially better
``coffee/cup/desk`` candidate at global trace rank 7, but generic primary-query results
consumed the scarce four Vision verdicts before that candidate was reviewed.

This module extends the existing Run183/Run214 owner seams instead of creating another
selector or retry loop:

* preserve every query/provider/rank source for a deduplicated candidate;
* leave the successful primary-attempt ordering untouched;
* use weighted reciprocal-rank fusion (RRF) only when recovery-rank evidence exists so
  corrective-query evidence can outrank generic primary-query hits after primary fails;
* turn severe multimodal Vision BLOCKs from the current attempt into bounded
  hard-negative semantic prototypes, then penalize similar recovery candidates locally;
* reset those hard negatives at the start of each selector audit scope so feedback cannot
  leak across beats or requests while remaining available to that selector's recovery;
* keep Run214's exact CanonicalVisualIntent authoritative for Vision;
* keep all Quality/Security/Cultural/Islamic thresholds and the four-review Vision ceiling
  unchanged;
* add no dependency, model download, provider, retry, or AI call.

The result applies wherever the shared Run183/Run214 visual selector is used. Short-specific
stock-query simplification remains owned by Run214 and is not expanded here.
"""

import hashlib
import json
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from typing import Callable

import isco_video_agent.visual_selection as visual_selection
from scripts import opening_feasibility_guard as opening_guard
from scripts import run183_visual_retrieval_closure as run183
from scripts import run214_canonical_visual_intent as run214
from scripts import short_cinematic_director as short_director
from scripts import vision_stage_contract_v2 as vision_contract
from scripts import visual_retrieval_adjudication_v1 as v1


CONTRACT_ID = "run215-weighted-rrf-vision-feedback-v1"
CONTRACT_VERSION = 1
RRF_K = 60
PRIMARY_STREAM_WEIGHT = 0.65
RECOVERY_STREAM_WEIGHT = 1.0
SEMANTIC_WEIGHT = 0.55
RRF_WEIGHT = 0.45
VISION_NEGATIVE_PENALTY = 0.22
DIVERSITY_PENALTY = 0.14
MAX_NEGATIVE_PROTOTYPES_PER_INTENT = 4
SEVERE_RELEVANCE_MAX = 0.25

_INSTALLED = False
_VISION_NEGATIVES: ContextVar[dict[str, tuple[frozenset[str], ...]] | None] = ContextVar(
    "isco_run215_vision_negatives",
    default=None,
)

_GENERIC_NEGATIVE_TOKENS = {
    "a", "an", "the", "and", "or", "of", "to", "in", "on", "at", "with", "from",
    "for", "by", "as", "is", "are", "be", "being", "been", "that", "this", "these",
    "those", "person", "people", "someone", "human", "adult", "man", "woman", "male",
    "female", "young", "old", "middle", "aged", "middleaged", "portrait", "video",
    "footage", "scene", "shot", "realistic", "cinematic", "vertical",
}


def _clean(value: object, limit: int = 500) -> str:
    return " ".join(str(value or "").split()).strip()[:limit]


def _intent_key(value: object) -> str:
    return _clean(value, 500).casefold()


def _negative_state() -> dict[str, tuple[frozenset[str], ...]]:
    current = _VISION_NEGATIVES.get()
    return dict(current or {})


def _record_vision_negative(
    *,
    intended_visual: str,
    provider: str,
    candidate: dict,
    audit: dict,
) -> tuple[str, ...]:
    if (
        str(audit.get("status") or "").casefold() != "block"
        or audit.get("vision_review_performed") is not True
    ):
        return ()
    try:
        relevance = float(audit.get("relevance") or 0.0)
    except (TypeError, ValueError):
        return ()
    if relevance > SEVERE_RELEVANCE_MAX:
        return ()

    intent = v1.build_visual_intent(intended_visual)
    intent_tokens = set(intent.expanded)
    candidate_tokens = set(v1._candidate_tokens(provider, candidate))
    prototype = frozenset(
        token
        for token in candidate_tokens
        if token not in intent_tokens and token not in _GENERIC_NEGATIVE_TOKENS and len(token) > 2
    )
    if len(prototype) < 2:
        return ()

    key = _intent_key(intended_visual)
    state = _negative_state()
    existing = list(state.get(key) or ())
    if prototype not in existing:
        existing.append(prototype)
    state[key] = tuple(existing[-MAX_NEGATIVE_PROTOTYPES_PER_INTENT:])
    _VISION_NEGATIVES.set(state)
    return tuple(sorted(prototype))


def _vision_feedback_audit_factory(current_factory: Callable):
    if getattr(current_factory, "_isco_run215_vision_feedback", False):
        return current_factory

    @wraps(current_factory)
    def factory(audit_fn, intended_visual: str):
        # The factory is invoked once at the start of each Opening/Section/Single/Short
        # selector scope. Reset here—not in recovery fusion—so primary Vision BLOCKs stay
        # available to that same selector's recovery, but never leak into the next beat.
        _VISION_NEGATIVES.set({})
        wrapped = current_factory(audit_fn, intended_visual)
        canonical = str(intended_visual or "").strip()

        @wraps(wrapped)
        def audit(*args, **kwargs):
            result = wrapped(*args, **kwargs)
            if not isinstance(result, dict):
                return result
            out = dict(result)
            candidate = kwargs.get("candidate")
            provider = str(kwargs.get("provider") or "").strip().lower()
            if isinstance(candidate, dict) and provider:
                prototype = _record_vision_negative(
                    intended_visual=canonical,
                    provider=provider,
                    candidate=candidate,
                    audit=out,
                )
                if prototype:
                    out["vision_feedback_prototype_recorded_run215"] = list(prototype)
                    out["vision_feedback_contract"] = CONTRACT_ID
            return out

        audit._isco_run215_vision_feedback = True
        return audit

    factory._isco_run215_vision_feedback = True
    factory._isco_run215_original = current_factory
    return factory


def _rank_source(query: str, provider: str, rank: int, phase: str) -> dict:
    return {
        "query": _clean(query, 260),
        "provider": str(provider).strip().lower(),
        "rank": max(1, int(rank)),
        "phase": phase,
    }


def _merge_with_rank_provenance(current: Callable):
    """Preserve Run183 dedup semantics while accumulating all retrieval-rank evidence."""
    if getattr(current, "_isco_run215_rank_provenance", False):
        return current

    @wraps(current)
    def wrapped(pools, *, excluded_pairs):
        merged = current(pools, excluded_pairs=excluded_pairs)
        if not isinstance(merged, dict):
            return merged

        by_identity: dict[tuple[str, object], dict] = {}
        by_url: dict[str, dict] = {}
        for provider, items in merged.items():
            if not isinstance(items, list):
                continue
            for candidate in items:
                if not isinstance(candidate, dict):
                    continue
                by_identity[run183._candidate_identity(provider, candidate)] = candidate
                url = str(candidate.get("url") or "").strip().casefold()
                if url:
                    by_url[url] = candidate

        for pool_index, entry in enumerate(pools or ()):
            if not isinstance(entry, tuple) or len(entry) != 2:
                continue
            query, pool = entry
            if not isinstance(pool, dict):
                continue
            phase = "primary" if pool_index == 0 else "recovery"
            for provider, items in pool.items():
                if not isinstance(items, list):
                    continue
                for rank, candidate in enumerate(items, 1):
                    if not isinstance(candidate, dict):
                        continue
                    target = by_identity.get(run183._candidate_identity(provider, candidate))
                    if target is None:
                        url = str(candidate.get("url") or "").strip().casefold()
                        if url:
                            target = by_url.get(url)
                    if target is None:
                        continue
                    meta = target.get("_isco_visual_intelligence")
                    if not isinstance(meta, dict):
                        meta = {}
                    queries = list(meta.get("retrieval_queries_v2") or ())
                    normalized_query = _clean(query, 260)
                    if normalized_query and normalized_query not in queries:
                        queries.append(normalized_query)
                    meta["retrieval_queries_v2"] = queries[: run183.MAX_ALTERNATE_QUERY_FANOUT + 1]

                    sources = list(meta.get("retrieval_rank_sources_run215") or ())
                    source = _rank_source(normalized_query, str(provider), rank, phase)
                    source_key = (source["query"], source["provider"], source["phase"])
                    existing_keys = {
                        (
                            str(item.get("query") or ""),
                            str(item.get("provider") or ""),
                            str(item.get("phase") or ""),
                        )
                        for item in sources
                        if isinstance(item, dict)
                    }
                    if source_key not in existing_keys:
                        sources.append(source)
                    meta["retrieval_rank_sources_run215"] = sources[:6]
                    target["_isco_visual_intelligence"] = meta
        return merged

    wrapped._isco_run215_rank_provenance = True
    wrapped._isco_run215_original = current
    return wrapped


def _rrf_normalized(meta: dict) -> float:
    total = 0.0
    for source in meta.get("retrieval_rank_sources_run215") or ():
        if not isinstance(source, dict):
            continue
        try:
            rank = max(1, int(source.get("rank") or 1))
        except (TypeError, ValueError):
            rank = 1
        phase = str(source.get("phase") or "").casefold()
        weight = RECOVERY_STREAM_WEIGHT if phase == "recovery" else PRIMARY_STREAM_WEIGHT
        total += weight / float(RRF_K + rank)
    return min(1.0, total * float(RRF_K + 1) / 1.8)


def _has_recovery_rank_evidence(rows: list) -> bool:
    for item in rows:
        if not isinstance(item, tuple) or len(item) != 2:
            continue
        candidate = item[1]
        if not isinstance(candidate, dict):
            continue
        meta = candidate.get("_isco_visual_intelligence")
        if not isinstance(meta, dict):
            continue
        for source in meta.get("retrieval_rank_sources_run215") or ():
            if isinstance(source, dict) and str(source.get("phase") or "").casefold() == "recovery":
                return True
    return False


def _negative_similarity(intent: v1.VisualIntent, provider: str, candidate: dict) -> float:
    prototypes = (_VISION_NEGATIVES.get() or {}).get(_intent_key(intent.raw)) or ()
    if not prototypes:
        return 0.0
    tokens = set(v1._candidate_tokens(provider, candidate)) - _GENERIC_NEGATIVE_TOKENS
    return max((v1._jaccard(tokens, set(proto)) for proto in prototypes), default=0.0)


def _rrf_feedback_rerank(current: Callable):
    """Compose on Run214's global rerank and change ordering only during recovery."""
    if getattr(current, "_isco_run215_rrf_feedback", False):
        return current

    @wraps(current)
    def wrapped(interleaved, intent: v1.VisualIntent):
        baseline = current(interleaved, intent)
        if not isinstance(baseline, list) or len(baseline) < 2:
            return baseline
        if not _has_recovery_rank_evidence(baseline):
            return baseline

        scored: list[tuple[float, int, str, dict]] = []
        for index, item in enumerate(baseline):
            if not isinstance(item, tuple) or len(item) != 2:
                continue
            provider, candidate = item
            if not isinstance(candidate, dict):
                continue
            meta = candidate.get("_isco_visual_intelligence")
            if not isinstance(meta, dict):
                meta = {}
            try:
                semantic = float(meta.get("global_retrieval_score_run214"))
            except (TypeError, ValueError):
                semantic = v1._candidate_relevance(
                    str(provider), candidate, intent, index, len(baseline)
                )
            rrf = _rrf_normalized(meta)
            negative = _negative_similarity(intent, str(provider), candidate)
            if meta.get("retrieval_rank_sources_run215"):
                score = SEMANTIC_WEIGHT * semantic + RRF_WEIGHT * rrf
            else:
                score = semantic
            score -= VISION_NEGATIVE_PENALTY * negative
            meta["rrf_score_run215"] = round(float(rrf), 6)
            meta["vision_negative_similarity_run215"] = round(float(negative), 6)
            meta["global_retrieval_score_run215"] = round(float(score), 6)
            candidate["_isco_visual_intelligence"] = meta
            scored.append((float(score), index, str(provider), candidate))

        remaining = sorted(scored, key=lambda row: (-row[0], row[1]))
        selected: list[tuple[float, int, str, dict]] = []
        selected_tokens: list[set[str]] = []
        while remaining:
            best_pos = 0
            best_value = -10.0
            for pos, row in enumerate(remaining):
                score, _index, provider, candidate = row
                tokens = v1._candidate_tokens(provider, candidate)
                similarity = max(
                    (v1._jaccard(tokens, previous) for previous in selected_tokens),
                    default=0.0,
                )
                value = score - DIVERSITY_PENALTY * similarity
                if value > best_value + 1e-12:
                    best_value = value
                    best_pos = pos
            chosen = remaining.pop(best_pos)
            selected.append(chosen)
            selected_tokens.append(v1._candidate_tokens(chosen[2], chosen[3]))

        ranked = [(provider, candidate) for _score, _index, provider, candidate in selected]
        if ranked:
            top_provider, top_candidate = ranked[0]
            meta = top_candidate.get("_isco_visual_intelligence") or {}
            print(
                "Run215 recovery-only weighted-RRF Vision-feedback rerank: "
                f"total={len(ranked)} top={top_provider}:{top_candidate.get('id')} "
                f"rrf={meta.get('rrf_score_run215', 0)} "
                f"negative={meta.get('vision_negative_similarity_run215', 0)} "
                f"score={meta.get('global_retrieval_score_run215', 0)}"
            )
        return ranked

    wrapped._isco_run215_rrf_feedback = True
    wrapped._isco_run215_original = current
    return wrapped


def _trace_row(current: Callable):
    if getattr(current, "_isco_run215_trace", False):
        return current

    @wraps(current)
    def wrapped(provider: str, candidate: dict, index: int) -> dict:
        row = dict(current(provider, candidate, index))
        meta = candidate.get("_isco_visual_intelligence")
        if not isinstance(meta, dict):
            meta = {}
        row["rank_sources_run215"] = list(meta.get("retrieval_rank_sources_run215") or ())
        row["rrf_score_run215"] = meta.get("rrf_score_run215")
        row["vision_negative_similarity_run215"] = meta.get("vision_negative_similarity_run215")
        row["global_score_run215"] = meta.get("global_retrieval_score_run215")
        return row

    wrapped._isco_run215_trace = True
    wrapped._isco_run215_original = current
    return wrapped


def _install_contract_fingerprint() -> None:
    current = vision_contract.vision_contract_fingerprint
    if getattr(current, "_isco_run215_contract", False):
        return

    @wraps(current)
    def wrapped() -> str:
        payload = {
            "base": current(),
            "contract_id": CONTRACT_ID,
            "contract_version": CONTRACT_VERSION,
            "module_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "fusion": "recovery-only-weighted-rrf-across-query-provider-rank-sources",
            "vision_feedback": "same-attempt-severe-block-prototype-local-penalty",
            "primary_stream_weight": PRIMARY_STREAM_WEIGHT,
            "recovery_stream_weight": RECOVERY_STREAM_WEIGHT,
            "vision_review_ceiling": visual_selection.MAX_VISION_REVIEWS_PER_SECTION,
            "extra_ai_calls": 0,
            "extra_dependencies": 0,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    wrapped._isco_run215_contract = True
    wrapped._isco_run215_original = current
    vision_contract.vision_contract_fingerprint = wrapped


def install_run215_visual_fusion() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    run183._merge_candidate_pools = _merge_with_rank_provenance(run183._merge_candidate_pools)
    run214._global_rerank = _rrf_feedback_rerank(run214._global_rerank)
    run214._candidate_trace_row = _trace_row(run214._candidate_trace_row)

    current_factory = opening_guard._stable_intent_audit
    feedback_factory = _vision_feedback_audit_factory(current_factory)
    opening_guard._stable_intent_audit = feedback_factory
    short_director._stable_intent_audit = feedback_factory

    _install_contract_fingerprint()
    _INSTALLED = True
    print(
        "Run215 Visual Fusion installed: dedup preserves query/provider ranks; primary "
        "ordering remains unchanged; weighted RRF activates only with recovery evidence; "
        "severe same-attempt Vision BLOCKs become bounded local hard-negative prototypes; "
        "Long+Short thresholds and four-review Vision ceiling unchanged; no extra AI "
        "call/dependency/model"
    )

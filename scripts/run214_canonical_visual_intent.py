from __future__ import annotations

"""Run214 canonical visual-intent and global retrieval closure.

Run214 proved that a Short can have healthy providers, a broad fused pool and four
completed Vision verdicts yet still fail because retrieval syntax escaped its proper
boundary. A generated recovery query (``aged arab box choos``) was both linguistically
broken and allowed to become the Vision "intended visual". The first Short visual also
failed before the post-core Run212/213 finishing scope could help.

This layer keeps one architecture for Long, standalone Shorts and source-derived sibling
Shorts:

* Canonical editorial intent is immutable and exact for Vision across primary and
  recovery search. StockSearchQuery and trusted semantic alternates are retrieval/ranking
  provenance only and are never promoted to editorial truth.
* Decision/choice morphology is detected from natural words (choose/choosing/choice/
  decision), so a stem such as ``choos`` can never become a stock query.
* Over-specified Short decision scenes receive one deterministic stock-retrievability
  simplification using safe-framed action anchors. Long keeps its existing primary search
  syntax and benefits only from canonical truth plus shared global reranking.
* The already-fused candidate list is globally reranked across providers before Vision,
  rather than allowing provider round-robin order to dominate the scarce four verdicts.
* The existing contact-sheet bytes and candidate ordering are attached to the first audit
  of each Short retrieval phase so failure diagnostics contain actual visual evidence and
  the complete ranked candidate path without another media download or Vision call.

No Quality/Vision/Security/Cultural/Islamic threshold, provider-attempt ceiling, query
fanout, or paid Vision ceiling changes here. Engine remains pinned and unchanged.
"""

import base64
import hashlib
import json
import math
import re
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from typing import Callable

import isco_video_agent.opening_director as opening_director
import isco_video_agent.orchestrator as orchestrator
import isco_video_agent.section_visual_sequence as section_visual_sequence
import isco_video_agent.visual_selection as visual_selection
from scripts import opening_feasibility_guard as opening_guard
from scripts import run183_visual_retrieval_closure as run183
from scripts import vision_stage_contract_v2 as vision_contract
from scripts import visual_retrieval_adjudication_v1 as v1
from scripts import visual_retrieval_runtime_scope_v1 as visual_scope


CONTRACT_ID = "run214-canonical-visual-intent-v2"
CONTRACT_VERSION = 2
MAX_TRACE_CANDIDATES = 260

_INSTALLED = False
_ACTIVE_FORMAT: ContextVar[str | None] = ContextVar("isco_run214_format", default=None)
_LAST_RANK_TRACE: ContextVar[list[dict] | None] = ContextVar("isco_run214_rank_trace", default=None)
_TRACE_ATTACHED: ContextVar[bool] = ContextVar("isco_run214_trace_attached", default=False)
_LAST_CONTACT_SHEET: ContextVar[bytes | None] = ContextVar("isco_run214_contact_sheet", default=None)

_DECISION_RE = re.compile(
    r"\b(?:choose|chooses|choosing|chosen|choice|choices|decide|decides|deciding|decision|decisions)\b",
    flags=re.I,
)
_SHORT_SPECIFICITY_TERMS = {
    "middle", "aged", "young", "old", "arab", "man", "woman", "person",
    "coffee", "phone", "smartphone", "dates", "box", "kitchen", "counter",
    "holding", "wearing", "cup", "mug",
}
_BROKEN_STEMS = {"choos"}
_DECISION_RECOVERY = (
    "hands choosing everyday items table",
    "decision choice everyday options",
)


def _runtime_active() -> bool:
    return bool(_ACTIVE_FORMAT.get()) or bool(visual_scope.active())


def _clean(value: object, limit: int = 500) -> str:
    return " ".join(str(value or "").split()).strip()[:limit]


def _decision_signal(value: object) -> bool:
    return bool(_DECISION_RE.search(str(value or "")))


def _contains_broken_stem(query: object) -> bool:
    tokens = set(re.findall(r"[a-z]+", str(query or "").casefold()))
    return bool(tokens & _BROKEN_STEMS)


def _natural_decision_family(current: Callable[..., run183.SemanticRetrievalFamily]):
    """Repair the Run214 morphology gap without changing the generic V1 stemmer."""

    @wraps(current)
    def wrapped(intended_visual: object, narration_context: object = ""):
        family = current(intended_visual, narration_context)
        combined = " ".join(
            part for part in (_clean(intended_visual), _clean(narration_context)) if part
        )
        if not _decision_signal(combined):
            return family

        variants: list[str] = []
        for query in (*_DECISION_RECOVERY, *family.alternates):
            normalized = opening_guard.stock_safe_search_query(query).strip()
            if (
                normalized
                and normalized != family.primary
                and not _contains_broken_stem(normalized)
                and normalized not in variants
            ):
                variants.append(normalized)
            if len(variants) >= run183.MAX_ALTERNATE_QUERY_FANOUT:
                break

        labels = set(family.labels)
        labels.add("decision")
        return run183.SemanticRetrievalFamily(
            primary=family.primary,
            alternates=tuple(variants),
            labels=frozenset(labels),
        )

    wrapped._isco_run214_natural_decision_family = True
    wrapped._isco_run214_original = current
    return wrapped


def _short_retrievable_query(current: Callable[[str], str]):
    """Simplify only over-constrained Short decision scenes before provider search."""

    @wraps(current)
    def wrapped(query: str) -> str:
        baseline = current(query)
        if str(_ACTIVE_FORMAT.get() or "").casefold() != "moment":
            return baseline
        raw = _clean(query, 1200)
        tokens = re.findall(r"[a-z]+", raw.casefold())
        specificity = sum(1 for token in tokens if token in _SHORT_SPECIFICITY_TERMS)
        if not _decision_signal(raw) or len(tokens) < 9 or specificity < 3:
            return baseline

        location = "desk" if any(token in {"desk", "notebook", "office", "work"} for token in tokens) else "table"
        simplified = f"hands choosing everyday items {location}"
        print(
            "Run214 stock-retrievability gate: "
            f"format=moment over_specific=true original={baseline} simplified={simplified}"
        )
        return simplified

    wrapped._isco_run214_short_retrievability = True
    wrapped._isco_run214_original = current
    return wrapped


def _canonical_judgment_intent(canonical: object, equivalents: tuple[str, ...]) -> str:
    """Return the exact editorial truth; equivalents are retrieval/ranking metadata only."""
    del equivalents
    return str(canonical or "").strip()


def _trace_snapshot() -> list[dict]:
    return list(_LAST_RANK_TRACE.get() or ())


def _canonical_intent_audit_factory(_historical_factory):
    """Keep the original editorial intent exact across Engine primary/recovery attempts.

    The pinned Engine intentionally uses the alternate search query as ``intended_visual``
    on its recovery call so its context-aware cache remains safe. This Runner seam keeps
    that retrieval string as provenance while restoring the original selector intent for
    the Vision judge. Semantic alternates remain available to retrieval/reranking only.
    """

    def factory(audit_fn, intended_visual: str):
        canonical = str(intended_visual or "").strip()

        @wraps(audit_fn)
        def wrapped(*args, **kwargs):
            retrieval_query = _clean(kwargs.get("intended_visual"))
            kwargs["intended_visual"] = _canonical_judgment_intent(canonical, ())
            _LAST_CONTACT_SHEET.set(None)
            try:
                result = audit_fn(*args, **kwargs)
            except Exception as exc:
                if not opening_guard._is_transient_vision_provider_failure(exc):
                    raise
                print(
                    "Vision provider call failed transiently, skipping this candidate: "
                    f"{type(exc).__name__}"
                )
                result = opening_guard._vision_provider_failure_envelope(exc)

            if not isinstance(result, dict):
                return result
            out = dict(result)
            out["canonical_visual_intent"] = canonical[:500]
            out["stock_search_query"] = retrieval_query[:260]
            out["vision_truth_contract"] = CONTRACT_ID
            out["search_query_promoted_to_editorial_truth"] = False

            if not _TRACE_ATTACHED.get():
                trace = _trace_snapshot()
                if trace:
                    out["ranked_candidate_trace"] = trace
                sheet = _LAST_CONTACT_SHEET.get()
                if sheet and str(_ACTIVE_FORMAT.get() or "").casefold() == "moment":
                    out["contact_sheet_media_type"] = "image/jpeg"
                    out["contact_sheet_sha256"] = hashlib.sha256(sheet).hexdigest()
                    out["contact_sheet_jpeg_base64"] = base64.b64encode(sheet).decode("ascii")
                _TRACE_ATTACHED.set(True)
            return out

        wrapped._isco_run214_canonical_visual_truth = True
        return wrapped

    factory._isco_run214_canonical_visual_truth = True
    factory._isco_run214_original = _historical_factory
    return factory


def _candidate_trace_row(provider: str, candidate: dict, index: int) -> dict:
    meta = candidate.get("_isco_visual_intelligence")
    if not isinstance(meta, dict):
        meta = {}
    return {
        "rank": int(index + 1),
        "provider": str(provider),
        "candidate_id": candidate.get("id"),
        "url": candidate.get("url") or candidate.get("pageURL"),
        "duration": candidate.get("duration"),
        "retrieval_score": meta.get("retrieval_score_v1"),
        "global_score": meta.get("global_retrieval_score_run214"),
        "retrieval_queries": list(meta.get("retrieval_queries_v2") or ()),
        "semantic_text": _clean(meta.get("semantic_text"), 260),
    }


def _global_rerank(interleaved: list[tuple[str, dict]], intent: v1.VisualIntent) -> list[tuple[str, dict]]:
    if len(interleaved) < 2 or not intent.raw:
        return list(interleaved)

    scored: list[tuple[float, int, str, dict]] = []
    total = len(interleaved)
    for index, (provider, candidate) in enumerate(interleaved):
        if not isinstance(candidate, dict):
            continue
        score = v1._candidate_relevance(provider, candidate, intent, index, total)
        scored.append((float(score), index, provider, candidate))

    remaining = sorted(scored, key=lambda item: (-item[0], item[1]))
    selected: list[tuple[float, int, str, dict]] = []
    selected_tokens: list[set[str]] = []
    while remaining:
        best_pos = 0
        best_value = -10.0
        for pos, item in enumerate(remaining):
            base, original_rank, provider, candidate = item
            tokens = v1._candidate_tokens(provider, candidate)
            similarity = max((v1._jaccard(tokens, prior) for prior in selected_tokens), default=0.0)
            value = base - 0.18 * similarity
            if value > best_value + 1e-12:
                best_value = value
                best_pos = pos
        chosen = remaining.pop(best_pos)
        base, original_rank, provider, candidate = chosen
        meta = candidate.get("_isco_visual_intelligence")
        if not isinstance(meta, dict):
            meta = {}
        meta["global_retrieval_score_run214"] = round(float(base), 6)
        meta["global_original_rank_run214"] = int(original_rank)
        candidate["_isco_visual_intelligence"] = meta
        selected.append(chosen)
        selected_tokens.append(v1._candidate_tokens(provider, candidate))

    return [(provider, candidate) for _score, _rank, provider, candidate in selected]


def _global_rank_wrapper(current):
    if getattr(current, "_isco_run214_global_rerank", False):
        return current

    @wraps(current)
    def wrapped(*args, **kwargs):
        interleaved = current(*args, **kwargs)
        if not _runtime_active() or not isinstance(interleaved, list):
            return interleaved
        intent = v1._INTENT.get()
        if intent is None:
            return interleaved
        ranked = _global_rerank(interleaved, intent)
        _LAST_RANK_TRACE.set(
            [
                _candidate_trace_row(provider, candidate, index)
                for index, (provider, candidate) in enumerate(ranked[:MAX_TRACE_CANDIDATES])
                if isinstance(candidate, dict)
            ]
        )
        _TRACE_ATTACHED.set(False)
        if ranked:
            top_provider, top_candidate = ranked[0]
            print(
                "Run214 global candidate rerank: "
                f"total={len(ranked)} top={top_provider}:{top_candidate.get('id')}"
            )
        return ranked

    wrapped._isco_run214_global_rerank = True
    wrapped._isco_run214_original = current
    return wrapped


def _contact_sheet_capture(current):
    if getattr(current, "_isco_run214_contact_sheet_capture", False):
        return current

    @wraps(current)
    def wrapped(preview: Path):
        frames = current(preview)
        if _runtime_active() and isinstance(frames, list) and len(frames) == 1:
            frame = frames[0]
            if isinstance(frame, (bytes, bytearray)) and frame:
                _LAST_CONTACT_SHEET.set(bytes(frame))
        return frames

    wrapped._isco_run214_contact_sheet_capture = True
    wrapped._isco_run214_original = current
    return wrapped


def _produce_scope(current):
    if getattr(current, "_isco_run214_visual_scope", False):
        return current

    @wraps(current)
    def wrapped(*args, **kwargs):
        fmt = str(kwargs.get("requested_format") or "").strip().casefold() or None
        token_format = _ACTIVE_FORMAT.set(fmt)
        token_trace = _LAST_RANK_TRACE.set(None)
        token_attached = _TRACE_ATTACHED.set(False)
        token_sheet = _LAST_CONTACT_SHEET.set(None)
        try:
            return current(*args, **kwargs)
        finally:
            _LAST_CONTACT_SHEET.reset(token_sheet)
            _TRACE_ATTACHED.reset(token_attached)
            _LAST_RANK_TRACE.reset(token_trace)
            _ACTIVE_FORMAT.reset(token_format)

    wrapped._isco_run214_visual_scope = True
    wrapped._isco_run214_original = current
    return wrapped


def _install_rank_hooks() -> None:
    current = visual_selection.rank_and_interleave
    wrapped = _global_rank_wrapper(current)
    visual_selection.rank_and_interleave = wrapped
    if opening_director.rank_and_interleave is current:
        opening_director.rank_and_interleave = wrapped
    if section_visual_sequence.rank_and_interleave is current:
        section_visual_sequence.rank_and_interleave = wrapped


def _install_contract_fingerprint() -> None:
    current = vision_contract.vision_contract_fingerprint
    if getattr(current, "_isco_run214_contract", False):
        return

    @wraps(current)
    def wrapped() -> str:
        payload = {
            "base": current(),
            "contract_id": CONTRACT_ID,
            "contract_version": CONTRACT_VERSION,
            "module_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "vision_truth": "canonical-intent-exact;retrieval-query-never-truth",
            "short_retrievability": "bounded-decision-safe-frame",
            "ranking": "global-cross-provider-mmr-before-vision",
            "vision_review_ceiling": visual_selection.MAX_VISION_REVIEWS_PER_SECTION,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    wrapped._isco_run214_contract = True
    wrapped._isco_run214_original = current
    vision_contract.vision_contract_fingerprint = wrapped


def install_run214_canonical_visual_intent() -> None:
    """Install after M7/Run183/V1 and before Opening Feasibility wraps selectors."""
    global _INSTALLED
    if _INSTALLED:
        return

    run183.semantic_query_family = _natural_decision_family(run183.semantic_query_family)
    opening_guard.stock_safe_search_query = _short_retrievable_query(
        opening_guard.stock_safe_search_query
    )
    opening_guard._stable_intent_audit = _canonical_intent_audit_factory(
        opening_guard._stable_intent_audit
    )
    _install_rank_hooks()
    vision_contract.legacy._sample_preview_frames = _contact_sheet_capture(
        vision_contract.legacy._sample_preview_frames
    )
    orchestrator.produce = _produce_scope(orchestrator.produce)
    _install_contract_fingerprint()
    _INSTALLED = True
    print(
        "Run214 Canonical Visual Intent installed: exact canonical Vision truth separated "
        "from StockSearchQuery; natural decision morphology; Short stock-retrievability gate; "
        "global cross-provider rerank; existing contact sheet + full shortlist embedded in "
        "Short failure audit; Vision/Security/Cultural thresholds and four-review ceiling unchanged"
    )
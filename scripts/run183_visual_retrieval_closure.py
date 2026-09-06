from __future__ import annotations

"""Run #183 Visual Retrieval closure, extended by the Run211 recovery-pool fix.

Run #183 proved that provider capacity and the final Vision judge were working, but the
retrieval layer could still collapse a rich editorial intent into a prop-level alternate
query (``marker closeup``) and could present the same provider asset to Vision twice across
primary/alternate pools. Run211 exposed the remaining composition gap: once recovery
opened, unreviewed primary candidates were stranded while the bounded Vision budget was
spent only on alternate-query results.

The production policy is:
- preserve one bounded alternate *phase* while allowing at most two deterministic
  semantic stock queries inside that phase;
- derive those queries from concept families, never from a single low-level prop;
- fuse every still-unreviewed primary candidate with the semantic alternate pools before
  the remaining Vision budget is spent;
- deduplicate the fused pool by provider+asset and page URL, then globally rerank it against
  the stable editorial intent so retrieval phase/order cannot dominate semantic fit;
- exclude every asset already inspected in the current selector before recovery results
  can reach Vision;
- enrich V1's local retrieval intent when a literal visual phrase implies a higher-level
  relationship/boundary concept;
- keep Vision as the mandatory final authority and keep the existing four-review ceiling.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Callable

import isco_video_agent.opening_director as opening_director
import isco_video_agent.orchestrator as orchestrator
import isco_video_agent.section_visual_sequence as section_visual_sequence
import isco_video_agent.visual_selection as visual_selection
from scripts import opening_feasibility_guard as opening_guard
from scripts import vision_stage_contract_v2 as contract
from scripts import visual_retrieval_adjudication_v1 as v1


CONTRACT_ID = "run183-visual-retrieval-closure-v1"
CONTRACT_VERSION = 2
MAX_ALTERNATE_QUERY_FANOUT = 2

_INSTALLED = False
_ORIGINAL_BUILD_VISUAL_INTENT: Callable[[object], v1.VisualIntent] | None = None

_RELATION_PATTERNS = (
    r"\bbetween\s+two\b",
    r"\btwo\s+people\b",
    r"\btwo\s+persons\b",
    r"\bconversation\b",
    r"\brelationship\b",
    r"\bpartner\b",
    r"\bcouple\b",
    r"\bfamily\b",
    r"\bfriends?\b",
    r"\bpersonal\s+space\b",
)
_BOUNDARY_TERMS = {
    "boundary", "boundaries", "limit", "limits", "line", "space", "distance",
    "separate", "separation", "door", "stop",
}
_RELATION_TERMS = {
    "relationship", "relationships", "conversation", "partner", "together", "space",
    "personal", "interaction", "distance",
}
_LOW_LEVEL_PROP_TERMS = {
    "marker", "markers", "whiteboard", "scribble", "scribbles", "drawing", "draw",
    "pen", "pencil", "camera", "lens", "aperture", "closeup", "close", "up",
}

# Ordered strategy catalog for recurring channel abstractions. These are retrieval hints,
# not semantic verdicts. Each query intentionally keeps at least one conceptual anchor
# and one scene/action anchor so stock search does not collapse to an isolated prop.
_STRATEGIES: tuple[tuple[frozenset[str], tuple[str, ...]], ...] = (
    (
        frozenset({"boundary", "relationship"}),
        (
            "personal boundaries calm conversation",
            "personal space relationship conversation",
        ),
    ),
    (
        frozenset({"boundary"}),
        (
            "personal boundaries space separation",
            "calm boundary gesture personal space",
        ),
    ),
    (
        frozenset({"relationship", "guilt"}),
        (
            "thoughtful relationship conversation hesitation",
            "calm difficult conversation personal space",
        ),
    ),
    (
        frozenset({"decision"}),
        (
            "decision choice crossroads direction",
            "choosing path difficult decision",
        ),
    ),
    (
        frozenset({"discipline"}),
        (
            "daily routine calendar discipline",
            "habit practice focused routine",
        ),
    ),
    (
        frozenset({"progress"}),
        (
            "progress stairs forward journey",
            "growth recovery moving forward",
        ),
    ),
    (
        frozenset({"comparison"}),
        (
            "comparison mirror race competition",
            "measuring progress against others",
        ),
    ),
    (
        frozenset({"pressure"}),
        (
            "stress pressure overwhelmed pause",
            "tension burden calm release",
        ),
    ),
    (
        frozenset({"focus"}),
        (
            "focus concentration notebook desk",
            "quiet study attention work",
        ),
    ),
    (
        frozenset({"calm"}),
        (
            "calm relief quiet release",
            "peace clarity resolved moment",
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class SemanticRetrievalFamily:
    primary: str
    alternates: tuple[str, ...]
    labels: frozenset[str]


def _base_intent(text: object) -> v1.VisualIntent:
    builder = _ORIGINAL_BUILD_VISUAL_INTENT or v1.build_visual_intent
    return builder(text)


def _raw_relation_signal(text: object) -> bool:
    raw = str(text or "").casefold()
    return any(re.search(pattern, raw) for pattern in _RELATION_PATTERNS)


def _concept_labels(text: object) -> set[str]:
    raw = str(text or "").casefold()
    base = _base_intent(text)
    expanded = set(base.expanded)
    labels: set[str] = set()
    if expanded & {v1._stem(item) for item in _BOUNDARY_TERMS}:
        labels.add("boundary")
    if _raw_relation_signal(raw) or expanded & {v1._stem(item) for item in _RELATION_TERMS}:
        labels.add("relationship")
    if expanded & {"guilt", "hesitation", "hesitant", "conflict", "uncertain", "pause"}:
        labels.add("guilt")
    if expanded & {"decision", "decide", "choice", "choose", "crossroad", "direction", "path"}:
        labels.add("decision")
    if expanded & {"discipline", "routine", "habit", "schedule", "calendar", "practice", "training"}:
        labels.add("discipline")
    if expanded & {"progress", "growth", "improve", "climb", "stairs", "journey", "forward", "recover", "rise"}:
        labels.add("progress")
    if expanded & {"comparison", "compare", "competition", "race", "mirror", "scale"}:
        labels.add("comparison")
    if expanded & {"pressure", "stress", "overwhelm", "tension", "burden", "weight"}:
        labels.add("pressure")
    if expanded & {"focus", "attention", "concentration", "desk", "notebook", "work", "study"}:
        labels.add("focus")
    if expanded & {"calm", "relief", "release", "peace", "quiet", "resolved", "clarity"}:
        labels.add("calm")
    return labels


def _enriched_build_visual_intent(text: object) -> v1.VisualIntent:
    base = _base_intent(text)
    raw = str(text or "")
    labels = _concept_labels(raw)
    anchors = set(base.anchors)
    expanded = set(base.expanded)

    # Run183 geometry: a literal "line/marker between two people" describes an abstract
    # relationship boundary. Keep the literal evidence available but stop low-level props
    # from dominating the local pre-ranker once the higher-level concept is unambiguous.
    if "boundary" in labels and "relationship" in labels:
        anchors.difference_update({v1._stem(item) for item in _LOW_LEVEL_PROP_TERMS})
        anchors.update({"boundary", "relationship", "conversation", "space"})
        expanded.update({"boundary", "relationship", "conversation", "space", "personal", "distance", "calm"})
    elif "relationship" in labels:
        anchors.update({"relationship", "conversation"})
        expanded.update({"relationship", "conversation", "personal", "space"})

    return v1.VisualIntent(
        raw=base.raw,
        anchors=frozenset(anchors),
        expanded=frozenset(expanded),
    )


def _safe_query(text: object) -> str:
    return opening_guard.stock_safe_search_query(str(text or "").strip()).strip()


def _generic_alternates(intent: v1.VisualIntent, primary: str) -> tuple[str, ...]:
    # Prefer conceptual terms over props. The order is deterministic so durable logs and
    # tests are stable across runs.
    useful = [
        token for token in sorted(intent.anchors)
        if token not in {v1._stem(item) for item in _LOW_LEVEL_PROP_TERMS}
        and token not in {"between", "thoughtful"}
    ]
    expanded = [
        token for token in sorted(intent.expanded)
        if token not in useful and token not in {v1._stem(item) for item in _LOW_LEVEL_PROP_TERMS}
    ]
    candidates: list[str] = []
    if useful:
        candidates.append(" ".join(useful[:4]))
    if expanded:
        mixed = list(dict.fromkeys([*useful[:2], *expanded[:3]]))
        if mixed:
            candidates.append(" ".join(mixed[:5]))
    out = []
    for query in candidates:
        normalized = _safe_query(query)
        if normalized and normalized != primary and normalized not in out:
            out.append(normalized)
    return tuple(out[:MAX_ALTERNATE_QUERY_FANOUT])


def semantic_query_family(
    intended_visual: object,
    narration_context: object = "",
) -> SemanticRetrievalFamily:
    intended = str(intended_visual or "").strip()
    primary = _safe_query(intended)
    combined = " ".join(part for part in (intended, str(narration_context or "")) if part.strip())
    intent = _enriched_build_visual_intent(combined)
    labels = _concept_labels(combined)

    variants: list[str] = []
    for required, queries in _STRATEGIES:
        if required.issubset(labels):
            for query in queries:
                normalized = _safe_query(query)
                if normalized and normalized != primary and normalized not in variants:
                    variants.append(normalized)
            if variants:
                break
    if not variants:
        variants.extend(_generic_alternates(intent, primary))

    # Never regress to the Run183 prop-only pattern. If a provider/LLM proposal later
    # supplies such a phrase, the selector wrapper ignores it unless it overlaps the
    # semantic family; this family itself never emits "closeup".
    variants = [query for query in variants if "closeup" not in query.casefold()]
    return SemanticRetrievalFamily(
        primary=primary,
        alternates=tuple(variants[:MAX_ALTERNATE_QUERY_FANOUT]),
        labels=frozenset(labels),
    )


def semantic_stock_query_ladder(query: str) -> tuple[str, ...]:
    family = semantic_query_family(query)
    if not family.primary:
        return ()
    # Opening Feasibility owns exactly one alternate-query slot. Return only the first
    # semantic alternate here; our selector wrapper fans out up to two bounded stock
    # queries inside that single phase before the Engine sees the merged pool.
    if family.alternates:
        return (family.primary, family.alternates[0])
    return (family.primary,)


def _candidate_identity(provider: object, candidate: object) -> tuple[str, object]:
    normalized_provider = str(provider or "").strip().lower()
    if not isinstance(candidate, dict):
        return normalized_provider, repr(candidate)
    asset_id = candidate.get("id")
    try:
        normalized_id: object = int(str(asset_id).strip())
    except (TypeError, ValueError):
        normalized_id = str(asset_id or candidate.get("url") or "").strip()
    return normalized_provider, normalized_id


def _cache_keys(cache: object) -> set[tuple[object, ...]]:
    store = getattr(cache, "_store", None)
    if not isinstance(store, dict):
        return set()
    return set(store.keys())


def _reviewed_pairs_since(cache: object, before: set[tuple[object, ...]]) -> set[tuple[str, object]]:
    pairs: set[tuple[str, object]] = set()
    for key in _cache_keys(cache) - set(before):
        if not isinstance(key, tuple) or len(key) < 2:
            continue
        pairs.add((str(key[0]).strip().lower(), key[1]))
    return pairs


def _explicit_excluded_pairs(exclude_ids: object) -> set[tuple[str, object]]:
    if not isinstance(exclude_ids, dict):
        return set()
    out: set[tuple[str, object]] = set()
    for provider, ids in exclude_ids.items():
        for asset_id in ids or ():
            try:
                normalized_id: object = int(str(asset_id).strip())
            except (TypeError, ValueError):
                normalized_id = asset_id
            out.add((str(provider).strip().lower(), normalized_id))
    return out


def _merge_candidate_pools(
    pools: list[tuple[str, dict[str, list[dict]]]],
    *,
    excluded_pairs: set[tuple[str, object]],
) -> dict[str, list[dict]]:
    merged: dict[str, list[dict]] = {}
    seen: set[tuple[str, object]] = set(excluded_pairs)
    seen_urls: set[str] = set()
    for query, pool in pools:
        if not isinstance(pool, dict):
            continue
        for provider, items in pool.items():
            if not isinstance(items, list):
                continue
            target = merged.setdefault(str(provider), [])
            for candidate in items:
                if not isinstance(candidate, dict):
                    continue
                identity = _candidate_identity(provider, candidate)
                url = str(candidate.get("url") or "").strip().casefold()
                if identity in seen or (url and url in seen_urls):
                    continue
                seen.add(identity)
                if url:
                    seen_urls.add(url)
                meta = candidate.get("_isco_visual_intelligence")
                if not isinstance(meta, dict):
                    meta = {}
                queries = list(meta.get("retrieval_queries_v2") or ())
                if query and query not in queries:
                    queries.append(query)
                meta["retrieval_queries_v2"] = queries[: MAX_ALTERNATE_QUERY_FANOUT + 1]
                candidate["_isco_visual_intelligence"] = meta
                target.append(candidate)
    return merged


def _rerank_recovery_pool(
    merged: dict[str, list[dict]],
    *,
    intended_visual: str,
    narration_context: str,
    family: SemanticRetrievalFamily,
) -> dict[str, list[dict]]:
    """Globally rerank the fused primary+alternate recovery pool before Vision.

    Provider searches remain bounded and Vision remains final authority. This is only a
    deterministic/local shortlist decision: the retrieval phase that happened to return a
    candidate must not decide whether it gets one of the remaining expensive reviews.
    """
    intent_text = " ".join(
        part
        for part in (
            intended_visual,
            narration_context,
            family.primary,
            *family.alternates,
        )
        if str(part or "").strip()
    )
    intent = _enriched_build_visual_intent(intent_text)
    reranked: dict[str, list[dict]] = {}
    for provider, items in merged.items():
        if isinstance(items, list):
            reranked[str(provider)] = v1.rerank_provider_candidates(str(provider), list(items), intent)
    return reranked


def _semantic_overlap(query: object, family: SemanticRetrievalFamily) -> bool:
    if not str(query or "").strip():
        return False
    if "closeup" in str(query).casefold():
        return False
    source = _enriched_build_visual_intent(" ".join((family.primary, *family.alternates)))
    candidate = _enriched_build_visual_intent(query)
    overlap = set(source.expanded) & set(candidate.expanded)
    return len(overlap) >= 2


def _wrap_selector(current, *, scope: str):
    if getattr(current, "_isco_run183_visual_retrieval_closure", False):
        return current

    @wraps(current)
    def wrapped(*args, **kwargs):
        intended = str(kwargs.get("intended_visual") or "").strip()
        narration = str(kwargs.get("narration_context") or "").strip()
        family = semantic_query_family(intended, narration)
        cache = kwargs.get("cache")
        cache_before = _cache_keys(cache)
        explicit_excluded = _explicit_excluded_pairs(kwargs.get("exclude_ids"))
        primary_candidates = args[0] if args else kwargs.get("candidates_by_provider")
        if not isinstance(primary_candidates, dict):
            primary_candidates = {}

        original_query_fn = kwargs.get("alternate_query_fn")
        if callable(original_query_fn):
            @wraps(original_query_fn)
            def semantic_alternate_query():
                proposed = str(original_query_fn() or "").strip()
                choices = list(family.alternates)
                normalized = _safe_query(proposed)
                if normalized and normalized != family.primary and _semantic_overlap(normalized, family):
                    if normalized not in choices:
                        choices.append(normalized)
                chosen = choices[0] if choices else ""
                if chosen:
                    print(
                        "Visual Retrieval Run183 semantic alternate: "
                        f"scope={scope} labels={','.join(sorted(family.labels)) or 'generic'} query={chosen}"
                    )
                return chosen

            kwargs["alternate_query_fn"] = semantic_alternate_query

        original_search_fn = kwargs.get("alternate_search_fn")
        if callable(original_search_fn):
            @wraps(original_search_fn)
            def semantic_alternate_search(query: str):
                variants = []
                normalized_input = _safe_query(query)
                if normalized_input and _semantic_overlap(normalized_input, family):
                    variants.append(normalized_input)
                for alternate in family.alternates:
                    if alternate not in variants:
                        variants.append(alternate)
                variants = variants[:MAX_ALTERNATE_QUERY_FANOUT]
                if not variants and normalized_input:
                    variants = [normalized_input]

                # Run211 closure: recovery is a candidate-fusion phase, not a hard switch
                # away from the primary retrieval result. Start with the primary pool so
                # every unreviewed primary candidate remains eligible for the remaining
                # Vision budget; reviewed candidates are removed below.
                pools: list[tuple[str, dict[str, list[dict]]]] = []
                if primary_candidates:
                    pools.append((family.primary, primary_candidates))
                for variant in variants:
                    result = original_search_fn(variant)
                    if isinstance(result, dict):
                        pools.append((variant, result))

                reviewed = _reviewed_pairs_since(cache, cache_before)
                merged = _merge_candidate_pools(
                    pools,
                    excluded_pairs=set(explicit_excluded) | reviewed,
                )
                merged = _rerank_recovery_pool(
                    merged,
                    intended_visual=intended,
                    narration_context=narration,
                    family=family,
                )
                counts = {
                    provider: len(items) for provider, items in merged.items() if isinstance(items, list)
                }
                print(
                    "Visual Retrieval Run183 recovery fusion: "
                    f"scope={scope} queries={variants} primary_included={bool(primary_candidates)} "
                    f"excluded_reviewed={len(reviewed)} unique_total={sum(counts.values())} "
                    f"by_provider={counts}"
                )
                return merged

            kwargs["alternate_search_fn"] = semantic_alternate_search

        return current(*args, **kwargs)

    wrapped._isco_run183_visual_retrieval_closure = True
    # Runtime Scope V1 relies on this attribute to recover the historical selector when
    # canonical Production is inactive. Preserve V1's original seam through our wrapper.
    wrapped._isco_visual_intent_original = getattr(current, "_isco_visual_intent_original", current)
    wrapped._isco_run183_original = current
    return wrapped


def _install_selector_hooks() -> None:
    current_opening = opening_director.select_opening_sequence
    wrapped_opening = _wrap_selector(current_opening, scope="opening")
    opening_director.select_opening_sequence = wrapped_opening
    if orchestrator.select_opening_sequence is current_opening:
        orchestrator.select_opening_sequence = wrapped_opening

    current_section = section_visual_sequence.select_section_sequence
    wrapped_section = _wrap_selector(current_section, scope="section")
    section_visual_sequence.select_section_sequence = wrapped_section
    if orchestrator.select_section_sequence is current_section:
        orchestrator.select_section_sequence = wrapped_section

    current_single = visual_selection.select_with_recovery
    wrapped_single = _wrap_selector(current_single, scope="single")
    visual_selection.select_with_recovery = wrapped_single
    if orchestrator.select_with_recovery is current_single:
        orchestrator.select_with_recovery = wrapped_single


def _install_intent_enrichment() -> None:
    global _ORIGINAL_BUILD_VISUAL_INTENT
    current = v1.build_visual_intent
    if getattr(current, "_isco_run183_visual_intent", False):
        return
    _ORIGINAL_BUILD_VISUAL_INTENT = current

    @wraps(current)
    def wrapped(text: object) -> v1.VisualIntent:
        return _enriched_build_visual_intent(text)

    wrapped._isco_run183_visual_intent = True
    wrapped._isco_run183_original = current
    v1.build_visual_intent = wrapped


def _install_query_policy() -> None:
    opening_guard.stock_query_ladder = semantic_stock_query_ladder


def _install_contract_fingerprint() -> None:
    current = contract.vision_contract_fingerprint
    if getattr(current, "_isco_run183_visual_retrieval", False):
        return

    @wraps(current)
    def wrapped() -> str:
        payload = {
            "base": current(),
            "contract_id": CONTRACT_ID,
            "contract_version": CONTRACT_VERSION,
            "module_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "alternate_query_fanout": MAX_ALTERNATE_QUERY_FANOUT,
            "dedup": "provider+asset-id+page-url+current-selector-reviewed",
            "query_policy": "semantic-family-no-prop-closeup",
            "recovery_pool_policy": "fuse-unreviewed-primary+semantic-alternates-then-rerank",
            "vision_review_ceiling": visual_selection.MAX_VISION_REVIEWS_PER_SECTION,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    wrapped._isco_run183_visual_retrieval = True
    wrapped._isco_run183_original = current
    contract.vision_contract_fingerprint = wrapped


def install_run183_visual_retrieval_closure() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _install_intent_enrichment()
    _install_query_policy()
    _install_selector_hooks()
    _install_contract_fingerprint()
    _INSTALLED = True
    print(
        "Run183 Visual Retrieval closure installed: semantic query families; max-two-query "
        "bounded alternate recall; Run211 fused unreviewed-primary recovery pool; global local "
        "rerank; current-selector asset exclusion; Long+Short Vision/Security gates and four-review "
        "ceiling unchanged"
    )

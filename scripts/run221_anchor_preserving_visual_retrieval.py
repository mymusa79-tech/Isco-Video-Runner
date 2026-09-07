from __future__ import annotations

"""Run #221 anchor-preserving visual retrieval closure.

Run221 proved that the canonical Vision intent could be specific (for example a person
at a wardrobe in the morning) while the bounded recovery query collapsed to style-only
language (``cinematic contemplative documentary front``). Vision then correctly rejected
morning landscapes and ambience because the provider pool no longer contained the
concrete scene anchors.

This adapter closes only that missing retrieval contract:
- keep Run183 as the bounded multi-query/fusion owner;
- keep Run215 as the weighted-RRF/feedback rerank owner;
- keep Run214 as the decision-scene retrievability owner;
- preserve concrete stock-index anchor families across primary and recovery queries;
- represent requested human presence with the existing safe-framing vocabulary instead
  of forcing an identifiable face;
- relax time/light context before any concrete anchor;
- never change the canonical intended_visual sent to Vision;
- add zero providers, AI calls, query phases, Vision reviews, or threshold changes.
"""

import hashlib
import json
import re
from functools import wraps
from pathlib import Path

from scripts import opening_feasibility_guard as opening_guard
from scripts import run183_visual_retrieval_closure as run183
from scripts import runtime_phase
from scripts import vision_stage_contract_v2 as vision_contract


CONTRACT_ID = "run221-anchor-preserving-visual-retrieval-v1"
CONTRACT_VERSION = 1
MAX_QUERY_TOKENS = 8

_INSTALLED = False

# Context is useful for the strict query, but it is the first thing to relax when recall
# is weak. None of these terms can satisfy the concrete-anchor contract by themselves.
_CONTEXT_TERMS = {
    "morning", "evening", "night", "dawn", "dusk", "sunrise", "sunset",
    "sunlight", "sunlit", "daylight", "moonlight", "bright", "dark", "warm", "calm",
}

# Provider-facing style/framing language must never crowd out a concrete scene anchor.
_LOW_VALUE_TERMS = {
    "cinematic", "documentary", "realistic", "style", "lighting", "soft", "natural",
    "minimalist", "quiet", "quietly", "contemplative", "reflective", "thoughtful",
    "subtle", "aesthetic", "atmospheric", "grounded", "portrait", "vertical", "shot",
    "scene", "visual", "image", "view", "front", "close", "closeup", "medium",
    "gentle", "hopeful", "meaningful", "payoff", "symbolic", "professional",
}

# Small stock-index ontology for recurring channel scenes. Each family keeps the literal
# canonical anchor and may add one provider-friendly equivalent. It never substitutes the
# canonical word away. This is deliberately conservative: unknown scenes fall back to the
# existing Run183/Run214 behavior rather than inventing a guessy hard constraint.
_ANCHOR_FAMILIES: dict[str, tuple[str, ...]] = {
    "wardrobe": ("wardrobe", "clothes", "closet", "outfit"),
    "closet": ("closet", "clothes", "wardrobe", "outfit"),
    "clothes": ("clothes", "clothing", "wardrobe", "outfit"),
    "clothing": ("clothing", "clothes", "wardrobe", "outfit"),
    "outfit": ("outfit", "clothes", "wardrobe", "clothing"),
    "shirt": ("shirt", "clothes", "wardrobe"),
    "shirts": ("shirts", "clothes", "wardrobe"),
    "phone": ("phone", "smartphone", "mobile"),
    "smartphone": ("smartphone", "phone", "mobile"),
    "notebook": ("notebook", "notes", "paper"),
    "paper": ("paper", "notebook", "notes"),
    "calendar": ("calendar", "schedule", "planner"),
    "mirror": ("mirror", "reflection"),
    "stairs": ("stairs", "steps", "staircase"),
    "crossroad": ("crossroad", "crossroads", "path", "direction"),
    "crossroads": ("crossroads", "crossroad", "path", "direction"),
    "desk": ("desk", "workspace", "table"),
    "table": ("table", "desk", "workspace"),
    "door": ("door", "doorway", "entrance"),
    "window": ("window", "room", "interior"),
    "bed": ("bed", "bedroom", "room"),
    "chair": ("chair", "seat", "room"),
    "book": ("book", "reading", "pages"),
    "coffee": ("coffee", "cup", "mug"),
    "cup": ("cup", "coffee", "mug"),
    "keys": ("keys", "key", "door"),
    "bag": ("bag", "backpack", "handbag"),
    "laptop": ("laptop", "computer", "desk"),
    "computer": ("computer", "laptop", "desk"),
    "kitchen": ("kitchen", "counter", "home"),
    "bedroom": ("bedroom", "room", "home"),
    "office": ("office", "workspace", "desk"),
}

_DECISION_RE = re.compile(
    r"\b(?:choose|chooses|choosing|chosen|choice|choices|decide|decides|deciding|decision|decisions)\b",
    flags=re.I,
)


def _unique_tokens(value: object) -> list[str]:
    return list(dict.fromkeys(re.findall(r"[a-z]+", str(value or "").casefold())))


def runtime_active() -> bool:
    """Production-only behavior, while still covering post-core Short finishing."""
    try:
        if runtime_phase.canonical_runtime_enabled():
            return True
    except Exception:
        pass
    try:
        from scripts import visual_retrieval_runtime_scope_v1 as visual_scope

        return bool(visual_scope.active())
    except Exception:
        return False


def _decision_owned_elsewhere(value: object) -> bool:
    """Run214 already owns over-specific decision/choice scene simplification."""
    return bool(_DECISION_RE.search(str(value or "")))


def _safe_subject(tokens: list[str]) -> tuple[str, ...]:
    human_terms = set(opening_guard.planner_quality_guard._HUMAN_QUERY_TERMS)
    safe_terms = set(opening_guard.planner_quality_guard._SAFE_FRAMING_TERMS)
    for token in tokens:
        if token in safe_terms:
            return (token,)
    if any(token in human_terms for token in tokens):
        return ("back",)
    return ()


def _canonical_anchors(tokens: list[str]) -> tuple[str, ...]:
    anchors: list[str] = []
    seen_families: set[frozenset[str]] = set()
    for token in tokens:
        family = _ANCHOR_FAMILIES.get(token)
        if not family:
            continue
        family_key = frozenset(family)
        if family_key in seen_families:
            continue
        seen_families.add(family_key)
        anchors.append(token)
        if len(anchors) >= 2:
            break
    return tuple(anchors)


def _context(tokens: list[str]) -> tuple[str, ...]:
    return tuple(token for token in tokens if token in _CONTEXT_TERMS)[:2]


def anchor_contract(value: object) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Return (safe_subject, concrete_anchors, relaxable_context)."""
    tokens = _unique_tokens(value)
    if not tokens or _decision_owned_elsewhere(value):
        return (), (), ()
    subject = _safe_subject(tokens)
    anchors = _canonical_anchors(tokens)
    if not subject or not anchors:
        return (), (), ()
    return subject, anchors, _context(tokens)


def _equivalents(anchor: str) -> set[str]:
    return set(_ANCHOR_FAMILIES.get(anchor, (anchor,)))


def preserves_anchor_contract(query: object, original: object) -> bool:
    subject, anchors, _ctx = anchor_contract(original)
    if not anchors:
        return True
    tokens = set(_unique_tokens(query))
    safe_terms = set(opening_guard.planner_quality_guard._SAFE_FRAMING_TERMS) | {"back"}
    if subject and not (tokens & safe_terms):
        return False
    return all(tokens & _equivalents(anchor) for anchor in anchors)


def _query_tokens(
    subject: tuple[str, ...],
    anchors: tuple[str, ...],
    context: tuple[str, ...],
    *,
    synonym_slot: int,
    alternate_subject: bool,
) -> list[str]:
    if alternate_subject and subject:
        subject_tokens = ["hands"] if "hands" not in subject else ["back"]
    else:
        subject_tokens = list(subject)

    out: list[str] = list(subject_tokens)
    for anchor in anchors:
        if anchor not in out:
            out.append(anchor)
        family = _ANCHOR_FAMILIES.get(anchor, (anchor,))
        if len(family) > synonym_slot:
            synonym = family[synonym_slot]
            if synonym not in out:
                out.append(synonym)
    for token in context:
        if token not in out:
            out.append(token)
    return out[:MAX_QUERY_TOKENS]


def anchor_query_family(original: object) -> tuple[str, tuple[str, ...]]:
    """Strict primary plus context-first controlled relaxation, max Run183 fanout."""
    subject, anchors, context = anchor_contract(original)
    if not anchors:
        return "", ()

    primary = " ".join(
        _query_tokens(subject, anchors, context, synonym_slot=1, alternate_subject=False)
    ).strip()
    candidates = (
        _query_tokens(subject, anchors, context[:1], synonym_slot=2, alternate_subject=True),
        _query_tokens(subject, anchors, (), synonym_slot=1, alternate_subject=False),
    )
    alternates: list[str] = []
    for tokens in candidates:
        query = " ".join(tokens).strip()
        if (
            query
            and query != primary
            and preserves_anchor_contract(query, original)
            and query not in alternates
        ):
            alternates.append(query)
        if len(alternates) >= run183.MAX_ALTERNATE_QUERY_FANOUT:
            break
    return primary, tuple(alternates)


def rewrite_semantic_family(
    family: run183.SemanticRetrievalFamily,
    intended_visual: object,
) -> run183.SemanticRetrievalFamily:
    primary, anchored_alternates = anchor_query_family(intended_visual)
    if not primary:
        return family

    variants: list[str] = list(anchored_alternates)
    for query in family.alternates:
        normalized = " ".join(_unique_tokens(query))
        if (
            normalized
            and normalized != primary
            and preserves_anchor_contract(normalized, intended_visual)
            and normalized not in variants
        ):
            variants.append(normalized)
        if len(variants) >= run183.MAX_ALTERNATE_QUERY_FANOUT:
            break

    labels = set(family.labels)
    labels.add("run221_anchor_preserved")
    out = run183.SemanticRetrievalFamily(
        primary=primary,
        alternates=tuple(variants[: run183.MAX_ALTERNATE_QUERY_FANOUT]),
        labels=frozenset(labels),
    )
    if out != family:
        _subject, anchors, context = anchor_contract(intended_visual)
        print(
            "Run221 anchor-preserving retrieval: "
            f"anchors={list(anchors)} context={list(context)} primary={out.primary} "
            f"alternates={list(out.alternates)}"
        )
    return out


def _wrap_stock_query(current):
    if getattr(current, "_isco_run221_anchor_stock_query", False):
        return current

    @wraps(current)
    def wrapped(query: str) -> str:
        baseline = current(query)
        if not runtime_active():
            return baseline
        primary, _alternates = anchor_query_family(query)
        return primary or baseline

    wrapped._isco_run221_anchor_stock_query = True
    wrapped._isco_run221_original = current
    return wrapped


def _wrap_semantic_family(current):
    if getattr(current, "_isco_run221_anchor_semantic_family", False):
        return current

    @wraps(current)
    def wrapped(intended_visual: object, narration_context: object = ""):
        family = current(intended_visual, narration_context)
        if not runtime_active():
            return family
        return rewrite_semantic_family(family, intended_visual)

    wrapped._isco_run221_anchor_semantic_family = True
    wrapped._isco_run221_original = current
    return wrapped


def _install_fingerprint() -> None:
    current = vision_contract.vision_contract_fingerprint
    if getattr(current, "_isco_run221_anchor_fingerprint", False):
        return

    @wraps(current)
    def wrapped() -> str:
        payload = {
            "base": current(),
            "contract_id": CONTRACT_ID,
            "contract_version": CONTRACT_VERSION,
            "module_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "query_policy": "canonical-anchor+one-synonym+context-first-relaxation",
            "max_query_tokens": MAX_QUERY_TOKENS,
            "max_alternate_fanout": run183.MAX_ALTERNATE_QUERY_FANOUT,
            "run214_decision_owner_preserved": True,
            "vision_thresholds_changed": False,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    wrapped._isco_run221_anchor_fingerprint = True
    wrapped._isco_run221_original = current
    vision_contract.vision_contract_fingerprint = wrapped


def install_run221_anchor_preserving_visual_retrieval() -> None:
    """Install one retrieval-only adapter over the existing certified owners."""
    global _INSTALLED
    if _INSTALLED:
        return
    opening_guard.stock_safe_search_query = _wrap_stock_query(
        opening_guard.stock_safe_search_query
    )
    run183.semantic_query_family = _wrap_semantic_family(run183.semantic_query_family)
    _install_fingerprint()
    _INSTALLED = True
    print(
        "Run221 Anchor-Preserving Visual Retrieval installed: concrete stock anchors survive "
        "bounded recovery; context relaxes first; Run183/Run215/Run214 and all Vision/Security/"
        "Cultural thresholds remain authoritative; zero new AI/query phases/review slots"
    )

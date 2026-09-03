from __future__ import annotations

"""Run #185 semantic Visual Intent adjudication closure.

Run #185 proved that Security, provider capacity and semantic retrieval were all working:
retrieval surfaced a clean man/woman conversation clip, but the final VLM still assigned
relevance=0.2 because it treated the planner's literal staging suggestion (three people +
a marker + a drawn line) as mandatory. The same run correctly rejected prop-only writing
and drawing clips. The missing boundary is therefore not a lower relevance threshold;
it is an explicit distinction between *semantic goal* and one optional literal depiction.

This module changes only the text contract supplied to the existing Visual Audit owner.
For curated Run183 semantic families it tells Gemini/Groq/OpenRouter which equivalent
stock-footage strategies are acceptable, while explicitly rejecting prop-only matches.
The Engine's exact relevance/quality thresholds, risk normalizer, Security preflight,
Vision budgets, semantic-BLOCK finality and provider order remain unchanged.
"""

import hashlib
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from typing import Any, Callable

import isco_video_agent.orchestrator as orchestrator

from scripts import run183_visual_retrieval_closure as run183
from scripts import vision_stage_contract_v2 as contract
from scripts.runtime_phase import canonical_runtime_enabled


CONTRACT_ID = "run185-visual-intent-adjudication-v1"
CONTRACT_VERSION = 1
_INSTALLED = False
_RUN185_ACTIVE: ContextVar[bool] = ContextVar("isco_run185_visual_intent_active", default=False)

# Only families backed by the curated Run183 strategy catalog are eligible. Generic
# token-derived alternates do not gain authority over the strict historical literal
# concept, which keeps this closure narrow and deterministic.
_SEMANTIC_GOALS: tuple[tuple[frozenset[str], str], ...] = (
    (frozenset({"boundary", "relationship"}), "healthy personal boundaries in relationships"),
    (frozenset({"boundary"}), "personal boundaries and respectful personal space"),
    (frozenset({"relationship", "guilt"}), "a difficult relationship conversation with hesitation or guilt"),
    (frozenset({"decision"}), "making a difficult choice or decision"),
    (frozenset({"discipline"}), "consistent discipline, routine, or habit practice"),
    (frozenset({"progress"}), "personal progress, recovery, or moving forward"),
    (frozenset({"comparison"}), "comparison with others or measuring progress"),
    (frozenset({"pressure"}), "stress, pressure, burden, and release"),
    (frozenset({"focus"}), "focus, concentration, or deliberate attention"),
    (frozenset({"calm"}), "calm, relief, clarity, or emotional release"),
)


def _semantic_goal(labels: frozenset[str]) -> str | None:
    for required, goal in _SEMANTIC_GOALS:
        if required.issubset(labels):
            return goal
    return None


def _compact_strategy(query: str, *, limit: int = 52) -> str:
    clean = " ".join(str(query or "").split())
    return clean[:limit].rstrip()


def build_adjudication_visual(
    intended_visual: object,
    narration_context: object = "",
) -> str:
    """Return a provider-facing semantic contract or the exact historical concept.

    The output is intentionally <=300 characters because both the pinned Engine Gemini
    prompt and the fallback prompt cap ``intended_visual`` at 300 characters.
    """
    original = " ".join(str(intended_visual or "").split())
    if not original:
        return original

    family = run183.semantic_query_family(original, narration_context)
    goal = _semantic_goal(family.labels)
    if not goal or not family.alternates:
        return original

    strategies = "; ".join(_compact_strategy(item) for item in family.alternates[:2])
    # Exact staging/props are explicitly optional, while prop-only coincidence is not
    # semantic equivalence. This is the Run185 distinction that keeps the 0.65 relevance
    # threshold meaningful rather than lowering it.
    text = (
        f"Semantic goal: {goal}. Accepted: {strategies}. "
        "Literal staging/props are optional and their absence is not a relevance failure. "
        "Prop-only drawing/marker footage without the goal is irrelevant."
    )
    if len(text) > 300:
        raise RuntimeError(f"Run185 adjudication contract exceeds provider prompt cap: {len(text)}")
    return text


def _install_route_contract() -> None:
    current = contract._route_visual_audit_v2
    if getattr(current, "_isco_run185_visual_intent", False):
        return

    @wraps(current)
    def routed(
        ledger,
        spec,
        provider: str,
        resolved_model: str,
        fn,
        *args: Any,
        **kwargs: Any,
    ):
        if _RUN185_ACTIVE.get():
            original = str(kwargs.get("intended_visual") or "")
            narration = str(kwargs.get("narration_context") or "")
            adjudication = build_adjudication_visual(original, narration)
            if adjudication and adjudication != original:
                kwargs = dict(kwargs)
                kwargs["intended_visual"] = adjudication
                family = run183.semantic_query_family(original, narration)
                print(
                    "Run185 Visual Intent adjudication: semantic equivalence enabled "
                    f"labels={','.join(sorted(family.labels))} literal_required=false"
                )
        return current(
            ledger,
            spec,
            provider,
            resolved_model,
            fn,
            *args,
            **kwargs,
        )

    routed._isco_run185_visual_intent = True
    routed._isco_run185_original = current
    contract._route_visual_audit_v2 = routed


def _install_contract_fingerprint() -> None:
    current = contract.vision_contract_fingerprint
    if getattr(current, "_isco_run185_visual_intent", False):
        return

    @wraps(current)
    def fingerprint() -> str:
        payload = "\x1f".join(
            (
                current(),
                CONTRACT_ID,
                str(CONTRACT_VERSION),
                hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "semantic-goal+curated-accepted-strategies+literal-optional+prop-only-reject",
            )
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    fingerprint._isco_run185_visual_intent = True
    fingerprint._isco_run185_original = current
    contract.vision_contract_fingerprint = fingerprint


def _install_produce_scope() -> None:
    current: Callable[..., Any] = orchestrator.produce
    if getattr(current, "_isco_run185_visual_intent_scope", False):
        return

    @wraps(current)
    def scoped_produce(*args: Any, **kwargs: Any):
        if not canonical_runtime_enabled():
            return current(*args, **kwargs)
        token = _RUN185_ACTIVE.set(True)
        try:
            return current(*args, **kwargs)
        finally:
            _RUN185_ACTIVE.reset(token)

    scoped_produce._isco_run185_visual_intent_scope = True
    scoped_produce._isco_run185_original = current
    orchestrator.produce = scoped_produce


def install_run185_visual_intent_adjudication() -> None:
    """Install the production-scoped semantic adjudication contract for Long + Short."""
    global _INSTALLED
    if _INSTALLED:
        return
    _install_route_contract()
    _install_contract_fingerprint()
    _install_produce_scope()
    _INSTALLED = True
    print(
        "Run185 Visual Intent adjudication installed: curated semantic goal + accepted "
        "equivalent strategies; literal props optional; Engine thresholds/Security/budgets unchanged"
    )

from __future__ import annotations

"""Promote only full Gold Text transport exhaustion to QC_PENDING.

The mature Final Critic transport remains authoritative for provider order, retries,
ledger accounting, schemas and semantic decisions. This module adds one narrow
classification seam: if the second/last Gold Text provider (OpenRouter) fails
technically, the release review has no valid semantic judgement and must surface a
typed provider-mesh outage instead of returning the Engine's generic fail-closed text
audit payload as though it were a content-quality BLOCK.

A valid provider response -- PASS or semantic BLOCK -- is never changed. In particular,
this does not shop providers after a semantic BLOCK and changes no score threshold.
"""

from contextvars import ContextVar
from functools import wraps

from isco_video_agent.ai_budget import Capability
from scripts import gold_final_critic_text_fallback as gold_text


CONTRACT_ID = "gold-text-qc-pending-v1"
CONTRACT_VERSION = 1

_ACTIVE_RELEASE_REVIEW: ContextVar[bool] = ContextVar(
    "isco_gold_text_qc_pending_release_review", default=False
)
_INSTALLED = False


class GoldTextProviderMeshUnavailableError(RuntimeError):
    """All registered Gold Text providers failed technically; no quality verdict exists."""


def _provider_review_with_qc_pending(current, audit_fn, provider: str, *args, **kwargs):
    result, provider_error = current(audit_fn, provider, *args, **kwargs)
    if (
        _ACTIVE_RELEASE_REVIEW.get()
        and provider == "openrouter"
        and provider_error is not None
    ):
        # OpenRouter is reached by the mature release-review function only after the
        # Gemini attempt failed technically. Do not include raw provider error text here:
        # it can contain request/provider details and the typed family is sufficient for
        # diagnostics, checkpointing and retry policy.
        raise GoldTextProviderMeshUnavailableError(
            "Gold Text provider mesh unavailable after all registered text reviewers failed technically"
        ) from provider_error
    return result, provider_error


def install_gold_text_qc_pending_v1() -> None:
    """Install the typed outage seam around the existing Gold release-review transport."""
    global _INSTALLED
    if _INSTALLED:
        return

    current_provider_review = gold_text._provider_review
    current_release_review = gold_text._release_review_with_fallback

    @wraps(current_provider_review)
    def classified_provider_review(audit_fn, provider: str, *args, **kwargs):
        return _provider_review_with_qc_pending(
            current_provider_review,
            audit_fn,
            provider,
            *args,
            **kwargs,
        )

    @wraps(current_release_review)
    def scoped_release_review(
        original_call_status,
        ledger,
        spec,
        provider,
        resolved_model,
        audit_fn,
        *args,
        **kwargs,
    ):
        is_gold_text = (
            getattr(spec, "task_id", "") == gold_text._GOLD_RELEASE_TASK
            and getattr(spec, "capability", None) is Capability.TEXT
        )
        if not is_gold_text:
            return current_release_review(
                original_call_status,
                ledger,
                spec,
                provider,
                resolved_model,
                audit_fn,
                *args,
                **kwargs,
            )
        token = _ACTIVE_RELEASE_REVIEW.set(True)
        try:
            return current_release_review(
                original_call_status,
                ledger,
                spec,
                provider,
                resolved_model,
                audit_fn,
                *args,
                **kwargs,
            )
        finally:
            _ACTIVE_RELEASE_REVIEW.reset(token)

    classified_provider_review._isco_gold_text_qc_pending_v1 = True
    classified_provider_review._isco_original = current_provider_review
    scoped_release_review._isco_gold_text_qc_pending_v1 = True
    scoped_release_review._isco_original = current_release_review
    gold_text._provider_review = classified_provider_review
    gold_text._release_review_with_fallback = scoped_release_review
    _INSTALLED = True
    print(
        "Gold Text QC_PENDING V1 installed: full technical text-mesh exhaustion is resumable; "
        "semantic BLOCK remains terminal"
    )

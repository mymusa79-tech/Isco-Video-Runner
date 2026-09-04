from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RetryDelayDecision:
    """One retry-owner decision for a provider-supplied minimum retry delay."""

    action: str
    delay_seconds: float | None
    provider_hint_seconds: float | None
    wait_budget_seconds: float
    reason: str


def parse_retry_after_seconds(value: object) -> float | None:
    """Parse a numeric Retry-After style delay without truncating provider evidence."""
    try:
        seconds = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


def retry_delay_decision(
    *,
    provider_hint: object = None,
    calculated_delay_seconds: float = 0.0,
    wait_budget_seconds: float,
) -> RetryDelayDecision:
    """Return a provider-owned retry delay or local backoff; never mix the two.

    A provider Retry-After owns the timing decision when it is present. If the full
    provider delay fits inside the local wait budget, retry after that exact delay.
    Do not add local exponential backoff or jitter on top of provider evidence.
    If the provider delay exceeds the local budget, fail over instead of shortening
    it. Only when no valid provider hint exists may local backoff/jitter be used and
    capped to the local budget.
    """
    budget = max(0.0, float(wait_budget_seconds))
    calculated = max(0.0, float(calculated_delay_seconds))
    hinted = parse_retry_after_seconds(provider_hint)

    if hinted is not None:
        if hinted > budget:
            return RetryDelayDecision(
                action="failover",
                delay_seconds=None,
                provider_hint_seconds=hinted,
                wait_budget_seconds=budget,
                reason="provider_retry_after_exceeds_local_wait_budget",
            )
        return RetryDelayDecision(
            action="retry",
            delay_seconds=hinted,
            provider_hint_seconds=hinted,
            wait_budget_seconds=budget,
            reason="provider_hint_honored",
        )

    return RetryDelayDecision(
        action="retry",
        delay_seconds=min(calculated, budget),
        provider_hint_seconds=None,
        wait_budget_seconds=budget,
        reason="local_backoff",
    )

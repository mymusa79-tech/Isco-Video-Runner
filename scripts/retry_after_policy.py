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
    """Return a full-delay retry or immediate failover; never a partial Retry-After.

    A provider Retry-After is a minimum safe delay. A local latency budget may decide
    that waiting that long is undesirable, but it must not shorten the provider delay
    and then issue the same request early. In that case the only valid local action is
    failover/no-same-provider-retry. Locally calculated backoff may be capped because it
    is our policy, not provider evidence, as long as the resulting delay still honors
    the complete provider hint.
    """
    budget = max(0.0, float(wait_budget_seconds))
    calculated = max(0.0, float(calculated_delay_seconds))
    hinted = parse_retry_after_seconds(provider_hint)

    if hinted is not None and hinted > budget:
        return RetryDelayDecision(
            action="failover",
            delay_seconds=None,
            provider_hint_seconds=hinted,
            wait_budget_seconds=budget,
            reason="provider_retry_after_exceeds_local_wait_budget",
        )

    local_delay = min(calculated, budget)
    delay = max(local_delay, hinted or 0.0)
    return RetryDelayDecision(
        action="retry",
        delay_seconds=delay,
        provider_hint_seconds=hinted,
        wait_budget_seconds=budget,
        reason="provider_hint_honored" if hinted is not None else "local_backoff",
    )

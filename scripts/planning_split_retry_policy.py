from __future__ import annotations

"""Run210 closure: retry transient split-outline failures per provider.

The shared Stage router already distinguishes retryable transient failures from
capacity/structural/semantic/internal failures. Split Outline historically configured
one attempt per provider, which meant that a Gemini timeout could not consume the
existing retry branch when another provider later failed for fixed capacity. This
module raises only the split-outline per-provider attempt allowance to two while
keeping the existing six-attempt request ceiling and every output/capacity/quality
budget unchanged.
"""

from scripts import planning_outline_split_contract as split
from scripts import planning_stage_contract as stage_contract


_INSTALLED = False
_ORIGINAL_POLICY = split._split_provider_policy


def split_retry_provider_policy() -> stage_contract.ProviderPolicy:
    """Return the existing split budgets with one bounded transient retry slot."""
    return stage_contract._provider_policy(
        split._COMPLETION_TOKENS,
        max_attempts_per_provider=2,
        max_total_attempts=stage_contract.OUTLINE_MAX_TOTAL_ATTEMPTS,
        second_pass_after_full_exhaustion=True,
        completion_tokens_by_provider=(("gemini", split._GEMINI_COMPLETION_TOKENS),),
    )


def install_planning_split_retry_policy() -> None:
    global _INSTALLED
    if _INSTALLED and split._split_provider_policy is split_retry_provider_policy:
        return
    split._split_provider_policy = split_retry_provider_policy
    _INSTALLED = True
    print(
        "Planning split retry policy installed: transient retries=2/provider "
        f"total_attempts={stage_contract.OUTLINE_MAX_TOTAL_ATTEMPTS}; "
        "capacity/structural/semantic failures remain terminal per provider"
    )

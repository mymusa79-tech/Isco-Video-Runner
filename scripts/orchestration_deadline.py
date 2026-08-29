from __future__ import annotations

"""Global deadline/admission foundation for Production Orchestration.

This module is intentionally not wired into production yet. A run owns one absolute
monotonic deadline. Each admitted stage gets one non-renewable lease bounded by the
stage local cap and the downstream reserve. Retries and child work consume that same
lease; they never mint fresh time.
"""

from dataclasses import dataclass
from enum import Enum
import time
from typing import Callable


class DeadlineContractError(ValueError):
    """Raised when a deadline policy or operation violates the budget contract."""


class AdmissionReason(str, Enum):
    ADMITTED = "ADMITTED"
    EXHAUSTED_DEADLINE = "EXHAUSTED_DEADLINE"


ClockMs = Callable[[], int]


def monotonic_ms() -> int:
    return time.monotonic_ns() // 1_000_000


@dataclass(frozen=True, slots=True)
class StageDeadlinePolicy:
    minimum_viable_ms: int
    local_cap_ms: int
    downstream_reserve_ms: int

    def __post_init__(self) -> None:
        if self.minimum_viable_ms <= 0:
            raise DeadlineContractError("minimum_viable_ms must be > 0")
        if self.local_cap_ms <= 0:
            raise DeadlineContractError("local_cap_ms must be > 0")
        if self.downstream_reserve_ms < 0:
            raise DeadlineContractError("downstream_reserve_ms must be >= 0")
        if self.local_cap_ms < self.minimum_viable_ms:
            raise DeadlineContractError(
                "local_cap_ms must be >= minimum_viable_ms"
            )


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    admitted: bool
    reason: AdmissionReason
    remaining_run_ms: int
    minimum_viable_ms: int
    downstream_reserve_ms: int
    stage_budget_ms: int
    lease: StageBudgetLease | None = None


@dataclass(frozen=True, slots=True)
class ChildDeadline:
    parent_deadline_at_ms: int
    deadline_at_ms: int
    created_at_ms: int

    def remaining_ms(self, *, clock_ms: ClockMs = monotonic_ms) -> int:
        return max(0, self.deadline_at_ms - clock_ms())


@dataclass(frozen=True, slots=True)
class StageBudgetLease:
    """One non-renewable stage budget shared by attempts and child work."""

    run_deadline_at_ms: int
    stage_deadline_at_ms: int
    admitted_at_ms: int
    initial_stage_budget_ms: int
    downstream_reserve_ms: int
    _clock_ms: ClockMs

    def remaining_ms(self) -> int:
        return max(0, self.stage_deadline_at_ms - self._clock_ms())

    def exhausted(self) -> bool:
        return self.remaining_ms() <= 0

    def timeout_ms(self, requested_timeout_ms: int) -> int:
        """Bound a provider/subprocess timeout by the live stage lease."""
        if requested_timeout_ms <= 0:
            raise DeadlineContractError("requested_timeout_ms must be > 0")
        remaining = self.remaining_ms()
        if remaining <= 0:
            raise DeadlineContractError("stage deadline exhausted")
        return min(requested_timeout_ms, remaining)

    def can_honor_retry_after(self, retry_after_ms: int) -> bool:
        """Never shorten provider Retry-After; report whether it fits in the same lease."""
        if retry_after_ms < 0:
            raise DeadlineContractError("retry_after_ms must be >= 0")
        return retry_after_ms <= self.remaining_ms()

    def derive_child_deadline(self, requested_cap_ms: int) -> ChildDeadline:
        """Derive a child deadline without extending the parent stage lease."""
        if requested_cap_ms <= 0:
            raise DeadlineContractError("requested_cap_ms must be > 0")
        now = self._clock_ms()
        if now >= self.stage_deadline_at_ms:
            raise DeadlineContractError("cannot derive child from exhausted stage lease")
        child_deadline = min(self.stage_deadline_at_ms, now + requested_cap_ms)
        return ChildDeadline(
            parent_deadline_at_ms=self.stage_deadline_at_ms,
            deadline_at_ms=child_deadline,
            created_at_ms=now,
        )


class RunDeadlineBudget:
    """Run-scoped absolute deadline authority.

    The absolute run deadline is computed exactly once at construction. Admission
    returns a single stage lease; callers must keep and reuse that lease for retries.
    """

    def __init__(
        self,
        configured_run_budget_ms: int,
        *,
        clock_ms: ClockMs = monotonic_ms,
    ) -> None:
        if configured_run_budget_ms <= 0:
            raise DeadlineContractError("configured_run_budget_ms must be > 0")
        self._clock_ms = clock_ms
        self._run_started_at_ms = clock_ms()
        self._run_deadline_at_ms = self._run_started_at_ms + configured_run_budget_ms
        self._configured_run_budget_ms = configured_run_budget_ms

    @property
    def run_started_at_ms(self) -> int:
        return self._run_started_at_ms

    @property
    def run_deadline_at_ms(self) -> int:
        return self._run_deadline_at_ms

    @property
    def configured_run_budget_ms(self) -> int:
        return self._configured_run_budget_ms

    def remaining_ms(self) -> int:
        return max(0, self._run_deadline_at_ms - self._clock_ms())

    def admit_stage(self, policy: StageDeadlinePolicy) -> AdmissionDecision:
        now = self._clock_ms()
        remaining = max(0, self._run_deadline_at_ms - now)
        required_to_start = policy.minimum_viable_ms + policy.downstream_reserve_ms
        if remaining < required_to_start:
            return AdmissionDecision(
                admitted=False,
                reason=AdmissionReason.EXHAUSTED_DEADLINE,
                remaining_run_ms=remaining,
                minimum_viable_ms=policy.minimum_viable_ms,
                downstream_reserve_ms=policy.downstream_reserve_ms,
                stage_budget_ms=0,
                lease=None,
            )

        usable_before_reserve = remaining - policy.downstream_reserve_ms
        stage_budget = min(policy.local_cap_ms, usable_before_reserve)
        if stage_budget < policy.minimum_viable_ms:
            return AdmissionDecision(
                admitted=False,
                reason=AdmissionReason.EXHAUSTED_DEADLINE,
                remaining_run_ms=remaining,
                minimum_viable_ms=policy.minimum_viable_ms,
                downstream_reserve_ms=policy.downstream_reserve_ms,
                stage_budget_ms=0,
                lease=None,
            )

        stage_deadline = now + stage_budget
        latest_allowed = self._run_deadline_at_ms - policy.downstream_reserve_ms
        if stage_deadline > latest_allowed:
            raise DeadlineContractError("stage deadline would consume downstream reserve")

        lease = StageBudgetLease(
            run_deadline_at_ms=self._run_deadline_at_ms,
            stage_deadline_at_ms=stage_deadline,
            admitted_at_ms=now,
            initial_stage_budget_ms=stage_budget,
            downstream_reserve_ms=policy.downstream_reserve_ms,
            _clock_ms=self._clock_ms,
        )
        return AdmissionDecision(
            admitted=True,
            reason=AdmissionReason.ADMITTED,
            remaining_run_ms=remaining,
            minimum_viable_ms=policy.minimum_viable_ms,
            downstream_reserve_ms=policy.downstream_reserve_ms,
            stage_budget_ms=stage_budget,
            lease=lease,
        )

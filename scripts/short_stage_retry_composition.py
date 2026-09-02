from __future__ import annotations

"""Compose native-Short stage identity with the existing bounded reset retry owner.

Run172 proved that the retry owner and the explicit native-Short Stage Contract were
individually correct but disagreed on what a retry means: the recovery layer may call
the same Draft/Review/Repair transport a second time after trustworthy Groq reset
evidence, while the Stage Contract counted that second transport attempt as a new
logical lifecycle stage.

This module does not add a retry or change any provider budget. It marks only the
already-authorized second attempt and makes the Stage Contract reuse the exact previous
stage identity without advancing its logical Draft -> Review / Repair call counter.
"""

from contextvars import ContextVar
from typing import Any, Callable, TypeVar

from scripts import native_short_stage_contract as short_stage
from scripts import planning_capacity_headroom as headroom
from scripts import planning_stage_contract as stage_contract


_T = TypeVar("_T")
_INSTALLED = False
_AUTHORIZED_RETRY: ContextVar[bool] = ContextVar(
    "isco_short_stage_authorized_terminal_retry",
    default=False,
)
_LAST_STAGE_KEY = "_isco_short_stage_retry_previous_stage"


def authorized_terminal_retry_active() -> bool:
    return bool(_AUTHORIZED_RETRY.get())


def _recovery_with_retry_identity(
    original_recovery: Callable[..., _T],
    call: Callable[[], _T],
    *,
    phase: str,
) -> _T:
    attempts = 0

    def tracked_call() -> _T:
        nonlocal attempts
        attempts += 1
        if attempts > 2:
            raise stage_contract.PlanningStageError(
                stage_contract.PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
                f"native Short terminal recovery exceeded certified attempts={attempts}",
                stage_id=f"planning.short_{phase}",
            )
        if attempts == 1:
            return call()
        token = _AUTHORIZED_RETRY.set(True)
        try:
            return call()
        finally:
            _AUTHORIZED_RETRY.reset(token)

    return original_recovery(tracked_call, phase=phase)


def _stage_with_retry_identity(
    original_stage_for_call: Callable[[dict[str, Any]], stage_contract.PlanningStageSpec],
    state: dict[str, Any],
) -> stage_contract.PlanningStageSpec:
    if authorized_terminal_retry_active():
        previous = state.get(_LAST_STAGE_KEY)
        if not isinstance(previous, stage_contract.PlanningStageSpec):
            raise stage_contract.PlanningStageError(
                stage_contract.PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
                "native Short authorized retry has no previous logical stage",
                stage_id="planning.short_retry_without_stage",
            )
        return previous

    spec = original_stage_for_call(state)
    state[_LAST_STAGE_KEY] = spec
    return spec


def install_short_stage_retry_composition() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    original_recovery = headroom._short_provider_call_with_terminal_recovery
    if not getattr(original_recovery, "_isco_short_stage_retry_composition", False):
        def recovery(call, *, phase: str):
            return _recovery_with_retry_identity(
                original_recovery,
                call,
                phase=phase,
            )

        recovery._isco_short_stage_retry_composition = True
        recovery._isco_short_stage_retry_original = original_recovery
        headroom._short_provider_call_with_terminal_recovery = recovery

    original_stage_for_call = short_stage._stage_for_call
    if not getattr(original_stage_for_call, "_isco_short_stage_retry_composition", False):
        def stage_for_call(state: dict[str, Any]):
            return _stage_with_retry_identity(original_stage_for_call, state)

        stage_for_call._isco_short_stage_retry_composition = True
        stage_for_call._isco_short_stage_retry_original = original_stage_for_call
        short_stage._stage_for_call = stage_for_call

    _INSTALLED = True
    print(
        "Short Stage/retry composition installed: "
        "logical_stage_reused_on_authorized_terminal_retry=true "
        "retry_budget=unchanged max_transport_attempts=2"
    )

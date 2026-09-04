from __future__ import annotations

"""Compose explicit native-Short operation identity with bounded terminal retry.

Run172 proved that the retry owner and the native-Short Stage Contract were individually
correct but disagreed on what a retry means: recovery may invoke the same
Draft/Review/Repair transport a second time after trustworthy Groq reset evidence, while
the Stage Contract must still treat both transport attempts as one logical operation.

This module does not add retries, change provider budgets, infer identity from call order,
or inspect prompt text. It marks only the already-authorized second attempt and reuses
the exact previous Stage Contract only when the currently active named operation matches
the operation that owned the first attempt.
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
_LAST_OPERATION_KEY = "_isco_short_stage_retry_previous_operation"


def authorized_terminal_retry_active() -> bool:
    return bool(_AUTHORIZED_RETRY.get())


def _active_operation_name() -> str | None:
    if short_stage.active_short_repair_context() is not None:
        return "short_repair"
    operation = short_stage.active_planning_operation()
    return str(operation).strip() if operation is not None else None


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
    original_stage_for_operation: Callable[[dict[str, Any]], stage_contract.PlanningStageSpec],
    state: dict[str, Any],
) -> stage_contract.PlanningStageSpec:
    operation = _active_operation_name()

    if authorized_terminal_retry_active():
        previous = state.get(_LAST_STAGE_KEY)
        previous_operation = state.get(_LAST_OPERATION_KEY)
        if not isinstance(previous, stage_contract.PlanningStageSpec) or not previous_operation:
            raise stage_contract.PlanningStageError(
                stage_contract.PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
                "native Short authorized retry has no previous logical operation",
                stage_id="planning.short_retry_without_stage",
            )
        if operation != previous_operation:
            raise stage_contract.PlanningStageError(
                stage_contract.PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
                f"native Short retry operation mismatch previous={previous_operation!r} current={operation!r}",
                stage_id="planning.short_retry_operation_mismatch",
            )
        return previous

    spec = original_stage_for_operation(state)
    operation = _active_operation_name()
    if operation not in {"short_draft", "short_review", "short_repair"}:
        raise stage_contract.PlanningStageError(
            stage_contract.PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
            f"native Short stage resolved without explicit named operation={operation!r}",
            stage_id="planning.short_retry_missing_operation",
        )
    state[_LAST_STAGE_KEY] = spec
    state[_LAST_OPERATION_KEY] = operation
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

    original_stage_for_operation = short_stage._stage_for_operation
    if not getattr(original_stage_for_operation, "_isco_short_stage_retry_composition", False):
        def stage_for_operation(state: dict[str, Any]):
            return _stage_with_retry_identity(original_stage_for_operation, state)

        stage_for_operation._isco_short_stage_retry_composition = True
        stage_for_operation._isco_short_stage_retry_original = original_stage_for_operation
        short_stage._stage_for_operation = stage_for_operation

    _INSTALLED = True
    print(
        "Short Stage/retry composition installed: "
        "named_operation_reused_on_authorized_terminal_retry=true "
        "ordinal_inference=false retry_budget=unchanged max_transport_attempts=2"
    )

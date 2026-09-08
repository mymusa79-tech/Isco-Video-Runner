from __future__ import annotations

"""Compose explicit native-Short operation identity with bounded terminal retry.

Run172 proved that the retry owner and the native-Short Stage Contract were individually
correct but disagreed on what a retry means: recovery may invoke the same
Draft/Review/Repair transport a second time after trustworthy Groq reset evidence, while
the Stage Contract must still treat both transport attempts as one logical operation.

Run227 exposed a second composition edge: the evidence-backed outer recovery waited for
the Groq TPM window reset, but the explicit Stage router retained its independent generic
30-second transient provider cooldown. The already-authorized second transport was then
rejected before Groq could be contacted. On that certified retry only, clear Groq's stale
transient Stage cooldown. Permanent circuits, other providers, attempt budgets, provider
capacity state, prompt contracts, and quality gates remain untouched.

This module does not add retries, change provider budgets, infer identity from call order,
or inspect prompt text. It marks only the already-authorized second attempt and reuses
the exact previous Stage Contract only when the currently active named operation matches
the operation that owned the first attempt.
"""

from contextvars import ContextVar
from typing import Any, Callable, TypeVar

import isco_video_agent.resilient_planner as staged

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
_ROUTER_MARKER = "_isco_explicit_planning_contract_router"
_TRANSIENT_COOLDOWN_FREEVAR = "transient_cooldown_until"


def authorized_terminal_retry_active() -> bool:
    return bool(_AUTHORIZED_RETRY.get())


def _active_operation_name() -> str | None:
    if short_stage.active_short_repair_context() is not None:
        return "short_repair"
    operation = short_stage.active_planning_operation()
    return str(operation).strip() if operation is not None else None


def _find_explicit_router_with_transient_state(
    candidate: object,
    seen: set[int] | None = None,
) -> Callable[..., Any] | None:
    """Find the actual explicit Stage router, not a wrapper that copied its marker."""
    if not callable(candidate):
        return None
    seen = set() if seen is None else seen
    identity = id(candidate)
    if identity in seen:
        return None
    seen.add(identity)

    code = getattr(candidate, "__code__", None)
    freevars = tuple(getattr(code, "co_freevars", ()) or ())
    if (
        getattr(candidate, _ROUTER_MARKER, False)
        and _TRANSIENT_COOLDOWN_FREEVAR in freevars
    ):
        return candidate

    wrapped = getattr(candidate, "__wrapped__", None)
    found = _find_explicit_router_with_transient_state(wrapped, seen)
    if found is not None:
        return found

    closure = tuple(getattr(candidate, "__closure__", ()) or ())
    for cell in closure:
        try:
            value = cell.cell_contents
        except ValueError:
            continue
        found = _find_explicit_router_with_transient_state(value, seen)
        if found is not None:
            return found
    return None


def _clear_groq_transient_cooldown_after_certified_reset() -> bool:
    """Remove only the stale generic Groq cooldown before the certified outer retry.

    The outer recovery has already validated Groq TPM reset evidence, slept through that
    reset, and cleared the model-scoped capacity window before it invokes transport #2.
    This bridge prevents the Stage router's longer generic transient timer from vetoing
    that same authorized retry. A permanent provider circuit is deliberately untouched.
    """
    explicit_router = _find_explicit_router_with_transient_state(staged.json_text)
    if explicit_router is None:
        print(
            "Short terminal reset re-admission: provider=groq "
            "stage_router_state=unavailable action=noop "
            "permanent_circuit_untouched=true retry_budget=unchanged"
        )
        return False

    code = getattr(explicit_router, "__code__", None)
    freevars = tuple(getattr(code, "co_freevars", ()) or ())
    closure = tuple(getattr(explicit_router, "__closure__", ()) or ())
    if len(freevars) != len(closure):
        print(
            "Short terminal reset re-admission: provider=groq "
            "stage_router_state=invalid action=noop "
            "permanent_circuit_untouched=true retry_budget=unchanged"
        )
        return False

    transient_state: object | None = None
    for name, cell in zip(freevars, closure):
        if name != _TRANSIENT_COOLDOWN_FREEVAR:
            continue
        try:
            transient_state = cell.cell_contents
        except ValueError:
            transient_state = None
        break

    if not isinstance(transient_state, dict):
        print(
            "Short terminal reset re-admission: provider=groq "
            "stage_router_transient_state=invalid action=noop "
            "permanent_circuit_untouched=true retry_budget=unchanged"
        )
        return False

    previous = transient_state.pop("groq", None)
    print(
        "Short terminal reset re-admission: provider=groq "
        f"stale_transient_cooldown_cleared={previous is not None} "
        "permanent_circuit_untouched=true retry_budget=unchanged"
    )
    return previous is not None


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

        # Reaching transport #2 is itself the authorization proof: the wrapped recovery
        # invokes it only after _terminal_reset_evidence() accepted Groq TPM reset data,
        # slept the bounded reset interval, and cleared the model-scoped capacity window.
        _clear_groq_transient_cooldown_after_certified_reset()
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
        "groq_transient_readmission=authorized_retry_only permanent_circuit_untouched=true "
        "ordinal_inference=false retry_budget=unchanged max_transport_attempts=2"
    )

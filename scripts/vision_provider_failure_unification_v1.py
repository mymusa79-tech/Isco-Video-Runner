from __future__ import annotations

"""Unify Vision transport failures with the shared provider failure taxonomy.

Run #240 exposed two related reliability gaps at Gold/Vision:

* Vision's transport classifier had its own small HTTP allow-list. A provider could
  return a valid transient status such as 520/521/522/523/524 and Vision would classify
  it as an internal contract failure instead of failing over.
* During Gold, Groq's explicit ``currently over capacity`` response should not trigger
  generic candidate-by-candidate half-open probing while the same model is saturated.

This closure does not change visual semantics, thresholds, provider order, attempt
budgets, or release authority. The shared HTTP taxonomy is global because transport
classification is provider-neutral; the stricter over-capacity cooldown is deliberately
scoped to Gold so ordinary retrieval keeps its certified bounded half-open recovery.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from typing import Callable, Iterator

from scripts import provider_health_registry as health
from scripts import vision_stage_contract_v2 as contract
from scripts.provider_failure import classify_provider_failure


CONTRACT_ID = "vision-provider-failure-unification-v1"
_INSTALLED = False
_GOLD_OVER_CAPACITY_COOLDOWN_ACTIVE: ContextVar[bool] = ContextVar(
    "isco_gold_over_capacity_cooldown_active",
    default=False,
)


def _shared_http_classification(status: int, message: str) -> contract.VisionErrorCode | None:
    """Translate only classifications the shared provider taxonomy proves."""
    detail = f"HTTP_{int(status)} status={int(status)} message={str(message or '')}"
    failure = classify_provider_failure("vision", detail)
    result = str(failure.telemetry_result or "").strip().lower()

    if result in {"server_error", "timeout", "network_error", "429"}:
        return contract.VisionErrorCode.PROVIDER_TRANSIENT
    if result == "auth_error":
        return contract.VisionErrorCode.AUTH_CONFIG
    if result in {"capacity_unavailable", "model_not_found", "payload_too_large"}:
        return contract.VisionErrorCode.CAPACITY
    if result in {"generation_error", "invalid_json"}:
        return contract.VisionErrorCode.STRUCTURAL_INVALID
    return None


def _install_http_classifier() -> None:
    current = contract._classify_http
    if getattr(current, "_isco_provider_failure_unification_v1", False):
        return

    @wraps(current)
    def unified_http_classifier(status: int, message: str) -> contract.VisionErrorCode:
        # Preserve every existing Vision-specific decision first. The shared taxonomy is
        # used only when the historical Vision classifier would otherwise report an
        # internal contract failure. This expands transport coverage without changing
        # certified schema/capacity/auth behavior.
        existing = current(int(status), str(message or ""))
        if existing is not contract.VisionErrorCode.INTERNAL_CONTRACT_ERROR:
            return existing
        shared = _shared_http_classification(int(status), str(message or ""))
        return shared if shared is not None else existing

    unified_http_classifier._isco_provider_failure_unification_v1 = True
    unified_http_classifier._isco_provider_failure_unification_original = current
    contract._classify_http = unified_http_classifier


def _explicit_over_capacity(reason: object) -> bool:
    text = str(reason or "").casefold()
    return (
        "currently over capacity" in text
        or "model over capacity" in text
        or "over capacity" in text
    )


@contextmanager
def gold_over_capacity_cooldown_scope() -> Iterator[None]:
    """Enable stricter saturation cooldown only while the enforcing Gold critic runs."""
    token = _GOLD_OVER_CAPACITY_COOLDOWN_ACTIVE.set(True)
    try:
        yield
    finally:
        _GOLD_OVER_CAPACITY_COOLDOWN_ACTIVE.reset(token)


def _classify_health_failure(
    reason: object,
    *,
    source: str,
    fallback: Callable[..., str],
) -> str:
    """Gold saturation cools down; ordinary Vision keeps its certified recovery policy."""
    if (
        _GOLD_OVER_CAPACITY_COOLDOWN_ACTIVE.get()
        and str(source or "").strip().lower() != "provider_preflight"
        and _explicit_over_capacity(reason)
    ):
        return health.FAILURE_RATE_LIMITED
    return fallback(reason, source=source)


def _install_over_capacity_health_policy() -> None:
    current = health._infer_failure_class
    if getattr(current, "_isco_provider_failure_unification_v1", False):
        return

    @wraps(current)
    def unified_health_classifier(reason: object, *, source: str) -> str:
        # Provider preflight remains authoritative. Only the enforcing Gold scope turns
        # explicit runtime saturation into the existing bounded RATE_LIMITED cooldown;
        # ordinary retrieval retains the existing transient half-open circuit behavior.
        return _classify_health_failure(
            reason,
            source=source,
            fallback=current,
        )

    unified_health_classifier._isco_provider_failure_unification_v1 = True
    unified_health_classifier._isco_provider_failure_unification_original = current
    health._infer_failure_class = unified_health_classifier


def install_vision_provider_failure_unification_v1() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _install_http_classifier()
    _install_over_capacity_health_policy()
    _INSTALLED = True
    print(
        "Vision Provider Failure Unification V1 installed: shared 5xx transport taxonomy; "
        "Gold-scoped model over-capacity cooldown; quality/provider-order/budgets unchanged"
    )

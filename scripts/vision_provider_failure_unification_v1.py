from __future__ import annotations

"""Unify Vision transport failures with the shared provider failure taxonomy.

Run #240 exposed two related reliability gaps at Gold/Vision:

* Vision's transport classifier had its own small HTTP allow-list. A provider could
  return a valid transient status such as 520/521/522/523/524 and Vision would classify
  it as an internal contract failure instead of failing over.
* Groq's explicit ``currently over capacity`` response was treated like a generic
  transient circuit. The generic half-open policy is intentionally eager, which can
  spend later visual candidates probing the same saturated model during one Gold run.

This closure does not change visual semantics, thresholds, provider order, attempt
budgets, or release authority. It only composes the existing shared provider taxonomy
into Vision and maps explicit model saturation onto the existing time-bounded
RATE_LIMITED health state.
"""

from functools import wraps

from scripts import provider_health_registry as health
from scripts import vision_stage_contract_v2 as contract
from scripts.provider_failure import classify_provider_failure


CONTRACT_ID = "vision-provider-failure-unification-v1"
_INSTALLED = False


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
    return "currently over capacity" in text or "model over capacity" in text or "over capacity" in text


def _install_over_capacity_health_policy() -> None:
    current = health._infer_failure_class
    if getattr(current, "_isco_provider_failure_unification_v1", False):
        return

    @wraps(current)
    def unified_health_classifier(reason: object, *, source: str) -> str:
        # Provider preflight is authoritative and must remain hard/fail-closed. Runtime
        # model saturation, however, needs a cooldown rather than candidate-by-candidate
        # half-open probes. Reuse the existing RATE_LIMITED state and its bounded retry
        # timestamp instead of inventing another circuit implementation.
        if str(source or "").strip().lower() != "provider_preflight" and _explicit_over_capacity(reason):
            return health.FAILURE_RATE_LIMITED
        return current(reason, source=source)

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
        "explicit model over-capacity uses bounded cooldown; quality/provider-order/budgets unchanged"
    )

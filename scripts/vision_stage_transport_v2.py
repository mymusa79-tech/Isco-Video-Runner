from __future__ import annotations

"""Transport hardening for Vision Stage Contract V2/V3.

The Stage Contract remains the semantic/schema/provider-policy owner. This module owns
only the raw HTTP boundary and the narrow composition adapters required by Run181:
- bind shared provider-health evidence to the existing run-scoped Vision circuit;
- preserve V2's TaskSpec provider-attempt budget when the V3 provider order is used.

No visual semantic rule, threshold, Security gate, candidate cap, or total inference
attempt ceiling is changed here.
"""

from dataclasses import replace
from functools import wraps

import requests

from scripts import provider_health_registry as health
from scripts import run181_vision_mesh_closure as run181
from scripts import vision_stage_contract_v2 as contract


def _classify_http(status: int, message: str) -> contract.VisionErrorCode:
    """Classify gateway HTTP failures by recovery scope, not by numeric code alone."""
    lowered = str(message or "").casefold()
    if status == 401:
        return contract.VisionErrorCode.AUTH_CONFIG
    if status == 403 and any(
        marker in lowered for marker in ("unauthorized", "api key", "invalid key", "permission")
    ):
        return contract.VisionErrorCode.AUTH_CONFIG
    # Run173 evidence: a free-model route can still return a balance/spend-cap 402.
    # That is capability/capacity unavailability, not an application contract bug.
    if status in {402, 403}:
        return contract.VisionErrorCode.CAPACITY
    if status == 404 and any(
        marker in lowered for marker in ("provider", "endpoint", "parameter", "model")
    ):
        return contract.VisionErrorCode.CAPACITY
    if status in {408, 409, 429, 500, 502, 503, 504}:
        return contract.VisionErrorCode.PROVIDER_TRANSIENT
    if status in {400, 404, 422} and any(
        marker in lowered for marker in ("schema", "parameter", "support", "modality")
    ):
        return contract.VisionErrorCode.CAPACITY
    return contract.VisionErrorCode.INTERNAL_CONTRACT_ERROR


def _install_transport_boundary() -> None:
    contract._classify_http = _classify_http

    current = contract._openrouter_call
    if getattr(current, "_isco_vision_transport_v2", False):
        return

    @wraps(current)
    def guarded_openrouter_call(*args, **kwargs):
        requested_model = str(kwargs.get("model") or contract.OPENROUTER_PRIMARY_MODEL)
        try:
            return current(*args, **kwargs)
        except contract.VisionStageError:
            raise
        except requests.Timeout as exc:
            raise contract.VisionStageError(
                contract.VisionErrorCode.PROVIDER_TRANSIENT,
                "OpenRouter transport timeout",
                provider="openrouter",
                requested_model=requested_model,
            ) from exc
        except requests.ConnectionError as exc:
            raise contract.VisionStageError(
                contract.VisionErrorCode.PROVIDER_TRANSIENT,
                "OpenRouter transport connection failure",
                provider="openrouter",
                requested_model=requested_model,
            ) from exc
        except requests.RequestException as exc:
            raise contract.VisionStageError(
                contract.VisionErrorCode.PROVIDER_TRANSIENT,
                f"OpenRouter transport request failure type={type(exc).__name__}",
                provider="openrouter",
                requested_model=requested_model,
            ) from exc

    guarded_openrouter_call._isco_vision_transport_v2 = True
    guarded_openrouter_call._isco_vision_transport_original = current
    contract._openrouter_call = guarded_openrouter_call


def _install_run181_route_adapter() -> None:
    """Preserve V2 budget ownership while binding V3 health to the same run scope."""
    current = contract._route_visual_audit_v2
    if getattr(current, "_isco_run181_route_adapter", False):
        return

    @wraps(current)
    def run181_route_adapter(
        ledger,
        spec,
        provider: str,
        resolved_model: str,
        fn,
        *args,
        **kwargs,
    ):
        # V2 already owns the canonical run-scoped Vision circuit. Reuse the exact
        # state object as the health lifecycle key, rather than creating a parallel
        # notion of a run. New scope => old health/certification evidence is discarded.
        state = contract.legacy._state()
        if health.bind_provider_health_to_vision_scope(state):
            run181._GROQ_MODEL_CERTIFIED.set(None)

        max_attempts = int(
            contract.VISION_STAGE_SPEC.provider_policy.max_total_inference_attempts
        )
        if max_attempts != 3:
            raise contract.VisionStageError(
                contract.VisionErrorCode.INTERNAL_CONTRACT_ERROR,
                f"Run181 route requires existing Vision attempt cap=3, found={max_attempts}",
                provider="internal",
            )

        # This is the exact V2 budget behavior: the logical Visual Audit TaskSpec is
        # allowed up to the existing three real provider attempts even if the Engine
        # supplied a narrower one-provider spec. Without this widening the local V3
        # router could correctly choose Groq/OpenRouter but BudgetLedger would deny the
        # second wire call. Skipped/circuit-open providers still spend zero attempts.
        routed_spec = replace(
            spec,
            max_provider_attempts=max(max_attempts, int(spec.max_provider_attempts)),
        )
        return current(
            ledger,
            routed_spec,
            provider,
            resolved_model,
            fn,
            *args,
            **kwargs,
        )

    run181_route_adapter._isco_run181_route_adapter = True
    run181_route_adapter._isco_run181_original = current
    contract._route_visual_audit_v2 = run181_route_adapter


def install_vision_provider_reliability() -> None:
    """Install HTTP hardening, Run181 mesh, budget adapter, then shared Stage owner."""
    _install_transport_boundary()
    run181.install_run181_vision_mesh_closure()
    _install_run181_route_adapter()
    contract.install_vision_provider_reliability()

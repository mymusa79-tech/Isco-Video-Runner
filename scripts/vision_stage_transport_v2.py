from __future__ import annotations

"""Transport hardening for Vision Stage Contract V2.

The Stage Contract remains the semantic/schema/provider-policy owner. This module owns
only the raw HTTP boundary so requests-level transport exceptions and provider
availability responses cannot bypass that taxonomy.
"""

from functools import wraps

import requests

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


def install_vision_provider_reliability() -> None:
    """Install HTTP hardening, then the shared Long+Short Vision Stage Contract V2."""
    _install_transport_boundary()
    contract.install_vision_provider_reliability()

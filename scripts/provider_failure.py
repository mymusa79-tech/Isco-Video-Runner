from __future__ import annotations

from dataclasses import dataclass

from isco_video_agent.ai_budget import AttemptOutcome


@dataclass(frozen=True)
class ProviderFailure:
    """Provider-neutral classification used by telemetry, budget, retries and circuits."""

    telemetry_result: str
    budget_outcome: AttemptOutcome
    open_circuit: bool


def classify_provider_failure(provider_name: str, error: Exception | str) -> ProviderFailure:
    """Classify provider failures by *scope*, not only by HTTP status.

    Run-wide circuits are reserved for conditions that cannot recover during the current
    production (auth/config/model/session quota). Request-size/output-shape failures stay
    request-scoped, while transient transport/rate/capacity failures remain retryable by
    the single outer Runner retry owner.
    """

    detail = str(error)
    lower = detail.lower()
    normalized_provider = "openrouter" if provider_name.startswith("openrouter") else provider_name

    # Capacity/model routing markers from the OpenRouter adapter are already normalized.
    if "openrouter_no_provider_available" in lower:
        return ProviderFailure("capacity_unavailable", AttemptOutcome.OTHER, True)
    if "openrouter_model_not_found" in lower or "model_not_found" in lower:
        return ProviderFailure("model_not_found", AttemptOutcome.OTHER, True)

    # A daily/project/key spend quota cannot heal inside this production run. Match it
    # before generic 429 so the router does not waste the bounded transient retry.
    quota_markers = (
        "quota_exceeded",
        "quota exceeded",
        "exceeded current quota",
        "daily quota",
        "insufficient quota",
        "spend limit",
        "spend cap",
        "key limit exceeded",
    )
    if any(marker in lower for marker in quota_markers):
        return ProviderFailure("429", AttemptOutcome.RATE_LIMITED, True)

    # Short-window throttling is transient. Keep the historical telemetry key `429`
    # for compatibility; the router may honor an explicit Retry-After before circuiting.
    if "429" in detail or "rate_limit_exceeded" in lower or "rate limit" in lower or "rate limited" in lower:
        return ProviderFailure("429", AttemptOutcome.RATE_LIMITED, True)

    # HTTP 413 describes this request payload, not provider session health.
    if (
        "413" in detail
        or "payload too large" in lower
        or "request too large" in lower
        or "payload_too_large_preflight" in lower
    ):
        return ProviderFailure("payload_too_large", AttemptOutcome.OTHER, False)

    if (
        "401" in detail
        or "403" in detail
        or "unauthorized" in lower
        or "forbidden" in lower
        or "authentication" in lower
        or "invalid api key" in lower
    ):
        return ProviderFailure("auth_error", AttemptOutcome.OTHER, True)

    # Invalid request/config is deterministic for this production adapter. Keep it
    # circuit-opening; request-size is separated above and therefore remains eligible
    # for a later smaller request.
    if "http 400" in lower or "bad request" in lower or "invalid argument" in lower or "parameter_unknown" in lower:
        return ProviderFailure("bad_request", AttemptOutcome.OTHER, True)

    # Groq documents 422 as potentially model-generation/semantic and retryable.
    if "422" in detail or "unprocessable entity" in lower or "model generation error" in lower:
        return ProviderFailure("generation_error", AttemptOutcome.OTHER, False)

    # Capacity-style failures that are explicitly temporary.
    if "498" in detail or "capacity_exceeded" in lower or "capacity exceeded" in lower:
        return ProviderFailure("server_error", AttemptOutcome.OTHER, False)

    if (
        "gemini_interaction_incomplete" in lower
        or "max_tokens" in lower
        or "max tokens" in lower
        or "finish_reason=length" in lower
        or "finish reason length" in lower
        or "premature" in lower
        or "truncated" in lower
        or "empty_output" in lower
        or "returned no choices" in lower
    ):
        return ProviderFailure("premature_response", AttemptOutcome.TRUNCATED, False)

    if (
        "safety" in lower
        or "recitation" in lower
        or "blocklist" in lower
        or "prohibited_content" in lower
        or "prohibited content" in lower
        or "spii" in lower
        or "model_armor" in lower
    ):
        return ProviderFailure("content_blocked", AttemptOutcome.CONTENT_BLOCKED, False)

    if (
        "invalid json" in lower
        or "complete json object" in lower
        or "gemini_empty_output" in lower
        or "malformed_function_call" in lower
        or "schema mismatch" in lower
    ):
        return ProviderFailure("invalid_json", AttemptOutcome.SCHEMA_INVALID, False)

    if "timeout" in lower or "timed out" in lower or "deadline_exceeded" in lower:
        return ProviderFailure("timeout", AttemptOutcome.TIMEOUT, False)

    # Transport libraries use several wordings for the same transient disconnect.
    network_markers = (
        "connection",
        "network",
        "disconnected",
        "connection reset",
        "remoteprotocolerror",
        "broken pipe",
        "unexpected eof",
        "eof occurred",
        "connection aborted",
    )
    if any(marker in lower for marker in network_markers):
        return ProviderFailure("network_error", AttemptOutcome.NETWORK_ERROR, False)

    if (
        any(code in detail for code in ("500", "502", "503", "504"))
        or "server error" in lower
        or "service_unavailable" in lower
        or "api_error" in lower
    ):
        return ProviderFailure("server_error", AttemptOutcome.OTHER, False)

    if normalized_provider == "openrouter" and "openrouter_not_found" in lower:
        return ProviderFailure("not_found", AttemptOutcome.OTHER, True)

    return ProviderFailure("other", AttemptOutcome.OTHER, False)

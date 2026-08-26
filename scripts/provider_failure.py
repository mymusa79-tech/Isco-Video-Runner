from __future__ import annotations

from dataclasses import dataclass

from isco_video_agent.ai_budget import AttemptOutcome


@dataclass(frozen=True)
class ProviderFailure:
    """One provider-neutral classification used by telemetry, budget, and circuits."""

    telemetry_result: str
    budget_outcome: AttemptOutcome
    open_circuit: bool


def classify_provider_failure(provider_name: str, error: Exception | str) -> ProviderFailure:
    """Classify a provider failure once, preserving one retry/fallback owner.

    Categories are stable Runner telemetry values. Session-permanent provider/config
    failures open that provider's circuit for the rest of the run. Output-shape and
    truncation failures remain eligible for another provider, while semantic content
    blocks are recorded distinctly from technical failures.
    """

    detail = str(error)
    lower = detail.lower()
    normalized_provider = "openrouter" if provider_name.startswith("openrouter") else provider_name

    if "openrouter_no_provider_available" in lower:
        return ProviderFailure("capacity_unavailable", AttemptOutcome.OTHER, True)

    if "openrouter_model_not_found" in lower:
        return ProviderFailure("model_not_found", AttemptOutcome.OTHER, True)

    if "429" in detail or "quota" in lower or "rate limit" in lower:
        return ProviderFailure("429", AttemptOutcome.RATE_LIMITED, True)

    if "413" in detail or "payload too large" in lower or "request too large" in lower:
        # Preserve the proven production policy: Groq 413 is session-permanent for
        # planning and must immediately fail over. Other providers gain explicit
        # telemetry but do not change circuit behavior in this remediation.
        return ProviderFailure(
            "payload_too_large",
            AttemptOutcome.OTHER,
            normalized_provider == "groq",
        )

    if (
        "401" in detail
        or "403" in detail
        or "unauthorized" in lower
        or "forbidden" in lower
        or "authentication" in lower
        or "invalid api key" in lower
    ):
        return ProviderFailure("auth_error", AttemptOutcome.OTHER, True)

    if "http 400" in lower or "bad request" in lower or "invalid argument" in lower:
        return ProviderFailure("bad_request", AttemptOutcome.OTHER, True)

    if (
        "gemini_interaction_incomplete" in lower
        or "max_tokens" in lower
        or "max tokens" in lower
        or "premature" in lower
        or "truncated" in lower
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
    ):
        return ProviderFailure("invalid_json", AttemptOutcome.SCHEMA_INVALID, False)

    if "timeout" in lower or "timed out" in lower:
        return ProviderFailure("timeout", AttemptOutcome.TIMEOUT, False)

    if "connection" in lower or "network" in lower:
        return ProviderFailure("network_error", AttemptOutcome.NETWORK_ERROR, False)

    if any(code in detail for code in ("500", "502", "503", "504")) or "server error" in lower:
        return ProviderFailure("server_error", AttemptOutcome.OTHER, False)

    if normalized_provider == "openrouter" and "openrouter_not_found" in lower:
        return ProviderFailure("not_found", AttemptOutcome.OTHER, True)

    return ProviderFailure("other", AttemptOutcome.OTHER, False)

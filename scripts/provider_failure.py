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
    """Classify a provider failure once, preserving current circuit policy.

    The category names are stable Runner telemetry values. BudgetLedger keeps its
    existing provider-neutral AttemptOutcome enum, so payload-too-large remains OTHER
    there while still becoming explicit in planning telemetry.
    """

    detail = str(error)
    lower = detail.lower()
    normalized_provider = "openrouter" if provider_name.startswith("openrouter") else provider_name

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

    if "invalid json" in lower or "complete json object" in lower:
        return ProviderFailure("invalid_json", AttemptOutcome.SCHEMA_INVALID, False)

    if "premature" in lower or "truncated" in lower:
        return ProviderFailure("premature_response", AttemptOutcome.TRUNCATED, False)

    if "timeout" in lower or "timed out" in lower:
        return ProviderFailure("timeout", AttemptOutcome.TIMEOUT, False)

    if "connection" in lower or "network" in lower:
        return ProviderFailure("network_error", AttemptOutcome.NETWORK_ERROR, False)

    return ProviderFailure("other", AttemptOutcome.OTHER, False)

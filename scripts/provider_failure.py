from __future__ import annotations

import re
from dataclasses import dataclass

from isco_video_agent.ai_budget import AttemptOutcome


@dataclass(frozen=True)
class ProviderFailure:
    """Provider-neutral classification used by telemetry, budget, retries and circuits."""

    telemetry_result: str
    budget_outcome: AttemptOutcome
    open_circuit: bool
    http_status: int | None = None
    retry_after_seconds: float | None = None
    quota_scope: str | None = None


class NoWireProviderFailure(RuntimeError):
    """A provider-route failure proven to have happened before HTTP transport.

    The explicit attribute is the shared accounting proof consumed by the Budget
    Ledger and Planning Stage Contract. Unknown exceptions intentionally do not gain
    this exemption and therefore remain fail-closed/countable.
    """

    wire_attempted = False

    def __init__(self, reason_code: str, detail: str) -> None:
        self.reason_code = str(reason_code).strip() or "NO_WIRE_LOCAL_FAILURE"
        super().__init__(f"NO_WIRE_LOCAL_FAILURE reason={self.reason_code} {detail}".strip())


def is_no_wire_provider_failure(error: BaseException) -> bool:
    """Return true only for an explicit boundary-owner proof of no HTTP attempt."""
    return getattr(error, "wire_attempted", None) is False


_HTTP_STATUS_PATTERNS = (
    re.compile(r"(?i)\bhttp(?:[_\s-]+error)?[_\s:=/-]*(?P<status>[1-5]\d{2})(?!\d)"),
    re.compile(r"(?i)\bstatus(?:[_\s-]+code)?\s*[:=]\s*(?P<status>[1-5]\d{2})(?!\d)"),
    re.compile(r"(?i)\berror\s+code\s*[:=]\s*(?P<status>[1-5]\d{2})(?!\d)"),
)


def _response_from_error(error: Exception | str):
    return getattr(error, "response", None) if not isinstance(error, str) else None


def _http_status(error: Exception | str, detail: str) -> int | None:
    """Extract only an explicitly-labelled HTTP status, never an arbitrary number."""
    candidates = (
        getattr(error, "status_code", None) if not isinstance(error, str) else None,
        getattr(_response_from_error(error), "status_code", None),
    )
    for candidate in candidates:
        try:
            status = int(candidate)
        except (TypeError, ValueError):
            continue
        if 100 <= status <= 599:
            return status
    for pattern in _HTTP_STATUS_PATTERNS:
        match = pattern.search(detail)
        if match:
            return int(match.group("status"))
    return None


def _retry_after_seconds(error: Exception | str, detail: str) -> float | None:
    sources = []
    if not isinstance(error, str):
        sources.extend((getattr(error, "headers", None), getattr(_response_from_error(error), "headers", None)))
    for headers in sources:
        if not hasattr(headers, "get"):
            continue
        raw = headers.get("Retry-After") or headers.get("retry-after")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value >= 0:
            return value
    patterns = (
        r"(?i)retry[-_\s]*after\s*[:=]?\s*(\d+(?:\.\d+)?)\s*s?",
        r"(?i)retry(?:ing)?\s+in\s+(\d+(?:\.\d+)?)\s*s(?:ec(?:ond)?s?)?",
        r"(?i)retry[_\s-]*delay[^0-9]{0,30}(\d+(?:\.\d+)?)\s*s",
    )
    for pattern in patterns:
        match = re.search(pattern, detail)
        if match:
            return float(match.group(1))
    return None


def _quota_scope(lower: str, retry_after: float | None) -> str:
    daily_markers = ("perday", "per day", "daily quota", "requests per day", " rpd")
    short_window_markers = (
        "perminute",
        "per minute",
        "requests per minute",
        "tokens per minute",
        " rpm",
        " tpm",
    )
    if any(marker in lower for marker in daily_markers):
        return "daily"
    if any(marker in lower for marker in short_window_markers) or retry_after is not None:
        return "short_window"
    return "unknown"


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
    http_status = _http_status(error, detail)
    retry_after = _retry_after_seconds(error, detail)

    # Capacity/model routing markers the Runner's own local circuit-breakers already
    # normalize (run125_capacity_routing_closure.py's preflight/structural OpenRouter
    # block - see its comment for why the two are distinct reasons under one marker).
    if "openrouter_unavailable_this_run" in lower:
        return ProviderFailure("capacity_unavailable", AttemptOutcome.OTHER, True)
    # A genuine OpenRouter API response saying no upstream provider/endpoint can serve
    # the requested model - distinct from the Runner-local marker above, and previously
    # unrecognized here: _safe_api_error() formats a real OpenRouter error as
    # "OPENROUTER_HTTP_<status> status=<n> code=<c> ... message=<m>", which never
    # contained the old literal "openrouter_no_provider_available" substring that only
    # the Runner's own local marker ever produced, so a real occurrence of this error
    # fell through to the generic "other" bucket below with open_circuit=False and was
    # silently retried instead of failed over.
    openrouter_no_provider_markers = (
        "no endpoints found",
        "no allowed providers",
        "no providers available",
        "no_endpoints_found",
        "no_providers_available",
    )
    if normalized_provider == "openrouter" and any(marker in lower for marker in openrouter_no_provider_markers):
        return ProviderFailure("capacity_unavailable", AttemptOutcome.OTHER, True)
    if "openrouter_model_not_found" in lower or "model_not_found" in lower:
        return ProviderFailure("model_not_found", AttemptOutcome.OTHER, True)

    # IMPORTANT ORDERING: some providers encode a request-specific TPM overflow as
    # HTTP 413 *and* include the text/code `rate_limit_exceeded`. Run #117 did exactly
    # that. Classify request capacity before generic rate-limit words so a request that
    # can never fit the configured envelope is not retried or allowed to poison the
    # provider circuit for later smaller batches.
    request_capacity_markers = (
        "groq_tpm_capacity_preflight",
        "payload_too_large_preflight",
        "payload too large",
        "request too large",
        "request too large for model",
    )
    if http_status == 413 or any(marker in lower for marker in request_capacity_markers):
        return ProviderFailure(
            "payload_too_large", AttemptOutcome.OTHER, False, http_status, retry_after
        )

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
        scope = _quota_scope(lower, retry_after)
        return ProviderFailure(
            "429",
            AttemptOutcome.RATE_LIMITED,
            scope != "short_window",
            http_status or 429,
            retry_after,
            scope,
        )

    # Short-window throttling is transient. Keep the historical telemetry key `429`
    # for compatibility; the router may honor an explicit Retry-After before circuiting.
    if http_status == 429 or "rate_limit_exceeded" in lower or "rate limit" in lower or "rate limited" in lower:
        scope = _quota_scope(lower, retry_after)
        return ProviderFailure(
            "429",
            AttemptOutcome.RATE_LIMITED,
            scope != "short_window",
            http_status or 429,
            retry_after,
            scope,
        )

    if (
        http_status in {401, 403}
        or "unauthorized" in lower
        or "forbidden" in lower
        or "authentication" in lower
        or "invalid api key" in lower
    ):
        return ProviderFailure("auth_error", AttemptOutcome.OTHER, True)

    # Run #118: Groq can return HTTP 400 json_validate_failed/structured_generation_failed
    # after accepting an otherwise valid GPT-OSS structured-output request. This is a
    # request-scoped generation failure, not an auth/model/config defect. Keep it eligible
    # for the single bounded provider retry/fallback and never poison Groq for later calls.
    if (
        "json_validate_failed" in lower
        or "structured_generation_failed" in lower
        or "failed to validate json" in lower
        or "groq_json_validate_failed" in lower
    ):
        return ProviderFailure("generation_error", AttemptOutcome.SCHEMA_INVALID, False)

    # Invalid request/config is deterministic for this production adapter. Keep it
    # circuit-opening; request-size and provider-side structured-generation failures are
    # separated above and therefore remain eligible for later bounded requests.
    if http_status == 400 or "bad request" in lower or "invalid argument" in lower or "parameter_unknown" in lower:
        return ProviderFailure("bad_request", AttemptOutcome.OTHER, True, http_status, retry_after)

    # Groq documents 422 as potentially model-generation/semantic and retryable.
    if http_status == 422 or "unprocessable entity" in lower or "model generation error" in lower:
        return ProviderFailure("generation_error", AttemptOutcome.OTHER, False, http_status, retry_after)

    # Capacity-style failures that are explicitly temporary.
    if http_status == 498 or "capacity_exceeded" in lower or "capacity exceeded" in lower:
        return ProviderFailure("server_error", AttemptOutcome.OTHER, False, http_status, retry_after)

    # An explicit upstream HTTP status is stronger evidence than incidental wording in
    # the provider message (for example Cloudflare's 522 body may also say "timeout" or
    # "connection"). Keep the whole 5xx family under one bounded transient class so
    # every adapter gets the same retry/failover behavior.
    if http_status is not None and 500 <= http_status <= 599:
        return ProviderFailure("server_error", AttemptOutcome.OTHER, False, http_status, retry_after)

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

    if "server error" in lower or "service_unavailable" in lower or "api_error" in lower:
        return ProviderFailure("server_error", AttemptOutcome.OTHER, False, http_status, retry_after)

    if normalized_provider == "openrouter" and "openrouter_not_found" in lower:
        return ProviderFailure("not_found", AttemptOutcome.OTHER, True)

    return ProviderFailure("other", AttemptOutcome.OTHER, False)

"""Provider reliability for the Telegram editorial *Research* call site only.

This module owns only Research provider transport reliability. It does not patch
Planning, change AI budgets, enable paid models, or relax quality/safety gates.

The reliability shape is deliberately bounded: Gemini gets at most one retry for
transient failures, then the request may fail over to OpenRouter's free-model
chain. If OpenRouter returns syntactically invalid JSON, one final schema-bound
free-model attempt is allowed. Safety blocks fail closed and never cross providers.

Provider diagnostics emitted here are intentionally metadata-only. They contain
provider, normalized failure class, numeric HTTP status when safely available,
numeric Retry-After when safely available, attempt, and action. Raw exception
messages, prompts, responses, headers, URLs, and credentials are never emitted.
"""

from __future__ import annotations

import random
import re
import time
from typing import Any

from scripts.provider_failure import ProviderFailure, classify_provider_failure
from scripts.retry_after_policy import retry_delay_decision
from isco_video_agent.providers.gemini import json_text as gemini_json_text
from isco_video_agent.providers.openrouter import json_text as openrouter_json_text

MAX_GEMINI_ATTEMPTS_FOR_TRANSIENT_FAILURE = 2
MIN_BACKOFF_SECONDS = 1.0
MAX_RETRY_AFTER_SECONDS = 15.0

_RETRY_AFTER_RE = re.compile(r"retry in (\d+(?:\.\d+)?)s", re.IGNORECASE)
_QUOTA_MARKERS = (
    "quota_exceeded",
    "quota exceeded",
    "exceeded current quota",
    "daily quota",
    "insufficient quota",
    "free_tier_requests",
)
_RETRYABLE_ON_SAME_PROVIDER = frozenset(
    {"429", "server_error", "network_error", "timeout", "capacity_unavailable"}
)

# Research is the only consumer of this schema. It mirrors the candidate contract
# already required by Engine research.py, without changing the Engine or any
# Production/Planning contract. The Engine still performs semantic filtering,
# diversity checks, exclusion checks, and the live-market evidence gate afterwards.
_SCORE_FIELDS = (
    "trend_score",
    "evergreen_score",
    "audience_fit",
    "emotional_pull",
    "title_thumbnail_potential",
    "hook_potential",
    "retention_potential",
    "competition_opportunity",
    "evidence_quality",
    "production_feasibility",
)
_RESEARCH_CANDIDATE_PROPERTIES: dict[str, Any] = {
    "title": {"type": "string", "minLength": 1},
    "market_query": {"type": "string", "minLength": 1},
    "pillar": {"type": "string", "enum": ["understand", "rise", "see"]},
    "format_hint": {"type": "string", "enum": ["film", "moment", "story"]},
    "evidence": {"type": "array", "items": {"type": "string"}},
}
for _score_field in _SCORE_FIELDS:
    _RESEARCH_CANDIDATE_PROPERTIES[_score_field] = {
        "type": "number",
        "minimum": 0,
        "maximum": 1,
    }

RESEARCH_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "minItems": 3,
            "maxItems": 10,
            "items": {
                "type": "object",
                "properties": _RESEARCH_CANDIDATE_PROPERTIES,
                "required": [
                    "title",
                    "market_query",
                    "pillar",
                    "format_hint",
                    *_SCORE_FIELDS,
                    "evidence",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["candidates"],
    "additionalProperties": False,
}


class ResearchProviderExhausted(RuntimeError):
    """All eligible live Research providers failed for one provider call."""


def _is_daily_quota_failure(error: BaseException) -> bool:
    detail = str(error).casefold()
    return any(marker in detail for marker in _QUOTA_MARKERS)


def _parsed_retry_after_seconds(error: BaseException) -> float | None:
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if headers is not None:
        try:
            raw = headers.get("Retry-After") or headers.get("retry-after")
            if raw is not None:
                value = float(raw)
                if value >= 0:
                    return value
        except (AttributeError, TypeError, ValueError):
            pass
    match = _RETRY_AFTER_RE.search(str(error))
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _http_status(error: BaseException) -> int | None:
    """Extract only a numeric HTTP status from structured exception metadata."""
    candidates = [getattr(error, "status_code", None), getattr(error, "status", None)]
    response = getattr(error, "response", None)
    if response is not None:
        candidates.extend(
            [getattr(response, "status_code", None), getattr(response, "status", None)]
        )
    for candidate in candidates:
        try:
            value = int(candidate)
        except (TypeError, ValueError):
            continue
        if 100 <= value <= 599:
            return value
    return None


def _format_optional_number(value: int | float | None) -> str:
    if value is None:
        return "none"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _emit_provider_failure(
    provider: str,
    error: BaseException,
    classification: ProviderFailure,
    *,
    attempt: int,
    action: str,
) -> None:
    """Emit a stable, non-sensitive provider-failure diagnostic line."""
    print(
        "RESEARCH_PROVIDER_TELEMETRY "
        f"provider={provider} "
        f"failure_class={classification.telemetry_result} "
        f"http_status={_format_optional_number(_http_status(error))} "
        f"retry_after_s={_format_optional_number(_parsed_retry_after_seconds(error))} "
        f"attempt={attempt} action={action}"
    )


def _backoff_seconds(error: BaseException, attempt: int) -> float | None:
    hinted = _parsed_retry_after_seconds(error)
    calculated = MIN_BACKOFF_SECONDS * attempt + random.uniform(0.0, 1.0)
    decision = retry_delay_decision(
        provider_hint=hinted,
        calculated_delay_seconds=calculated,
        wait_budget_seconds=MAX_RETRY_AFTER_SECONDS,
    )
    return decision.delay_seconds if decision.action == "retry" else None


def _is_eligible_for_fallback(telemetry_result: str) -> bool:
    return telemetry_result != "content_blocked"


def _structured_openrouter_retry(
    prompt: str,
    *,
    fallback_models: tuple[str, ...],
    default_model: str,
) -> dict[str, Any]:
    """Use one explicit free model with strict schema after invalid JSON only."""
    structured_model = fallback_models[0] if fallback_models else default_model
    return openrouter_json_text(
        prompt,
        model=structured_model,
        fallback_models=(),
        response_schema=RESEARCH_RESPONSE_SCHEMA,
        schema_name="isco_topic_research_response",
    )


def gemini_research_call_with_fallback(
    api_key: str,
    prompt: str,
    model: str,
    *,
    openrouter_model: str = "openrouter/free",
    openrouter_fallback_models: tuple[str, ...] = ("openai/gpt-oss-20b:free",),
) -> dict[str, Any]:
    """Run one live Research call with bounded Gemini retry and free failover."""
    attempt = 0
    last_gemini_class = "none"
    last_gemini_status: int | None = None
    while True:
        attempt += 1
        try:
            return gemini_json_text(api_key, prompt, model=model)
        except Exception as exc:  # noqa: BLE001 - normalized immediately below
            classification = classify_provider_failure("gemini", exc)
            last_gemini_class = classification.telemetry_result
            last_gemini_status = _http_status(exc)
            quota = _is_daily_quota_failure(exc)
            can_retry_same_provider = (
                not quota
                and classification.telemetry_result in _RETRYABLE_ON_SAME_PROVIDER
                and attempt < MAX_GEMINI_ATTEMPTS_FOR_TRANSIENT_FAILURE
            )
            if can_retry_same_provider:
                delay = _backoff_seconds(exc, attempt)
                if delay is not None:
                    _emit_provider_failure(
                        "gemini", exc, classification, attempt=attempt, action="retry"
                    )
                    time.sleep(delay)
                    continue
                _emit_provider_failure(
                    "gemini",
                    exc,
                    classification,
                    attempt=attempt,
                    action="failover_retry_after_budget",
                )
            elif not _is_eligible_for_fallback(classification.telemetry_result):
                _emit_provider_failure(
                    "gemini", exc, classification, attempt=attempt, action="fail_closed"
                )
                raise
            else:
                _emit_provider_failure(
                    "gemini", exc, classification, attempt=attempt, action="failover"
                )
            if not _is_eligible_for_fallback(classification.telemetry_result):
                raise
            break

    try:
        return openrouter_json_text(
            prompt,
            model=openrouter_model,
            fallback_models=openrouter_fallback_models,
        )
    except Exception as exc:  # noqa: BLE001 - normalized immediately below
        classification = classify_provider_failure("openrouter", exc)
        if classification.telemetry_result == "invalid_json":
            _emit_provider_failure(
                "openrouter", exc, classification, attempt=1, action="retry_structured"
            )
            try:
                return _structured_openrouter_retry(
                    prompt,
                    fallback_models=openrouter_fallback_models,
                    default_model=openrouter_model,
                )
            except Exception as structured_exc:  # noqa: BLE001 - normalized below
                structured_classification = classify_provider_failure(
                    "openrouter", structured_exc
                )
                _emit_provider_failure(
                    "openrouter",
                    structured_exc,
                    structured_classification,
                    attempt=2,
                    action="exhausted",
                )
                openrouter_status = _http_status(structured_exc)
                raise ResearchProviderExhausted(
                    "research_provider_exhausted "
                    f"gemini_failure_class={last_gemini_class} "
                    f"gemini_http_status={_format_optional_number(last_gemini_status)} "
                    f"openrouter_first_failure_class={classification.telemetry_result} "
                    f"openrouter_failure_class={structured_classification.telemetry_result} "
                    f"openrouter_http_status={_format_optional_number(openrouter_status)}"
                ) from structured_exc

        _emit_provider_failure(
            "openrouter", exc, classification, attempt=1, action="exhausted"
        )
        openrouter_status = _http_status(exc)
        raise ResearchProviderExhausted(
            "research_provider_exhausted "
            f"gemini_failure_class={last_gemini_class} "
            f"gemini_http_status={_format_optional_number(last_gemini_status)} "
            f"openrouter_failure_class={classification.telemetry_result} "
            f"openrouter_http_status={_format_optional_number(openrouter_status)}"
        ) from exc
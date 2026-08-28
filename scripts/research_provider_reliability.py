"""Provider reliability for the Telegram editorial *Research* call site only.

Scope discipline: this module exists because Research (topic-candidate discovery,
run from ``scripts/telegram_control_active_ui.py::_research_current`` via
``scripts/telegram_control_simple_ui.py::_english_research_queries``) has never had
any of the provider-failure protection that Planning already has (see
docs/PLANNING_PROVIDER_RELIABILITY_V2_2026-08-26.md and
docs/RUN116_PLANNING_PORTABILITY_2026-08-26.md). It intentionally does NOT import,
patch, or otherwise touch any Planning module (task_level_planner_router.py,
provider_capacity_hardening.py, run_v3_voice.py, resilient_planner.py, or anything
under isco_video_agent's planning/orchestrator path). It reuses only the one
provider-neutral, side-effect-free classifier already shared across this codebase
(``scripts.provider_failure.classify_provider_failure``) plus the canonical
non-truncating Retry-After policy.

Design, matching the same "one retry owner, bounded backoff, then fail over" shape
already applied to Planning, scoped to a single Research provider call:

- A daily/session quota failure (Gemini free-tier "quota exceeded" / "daily quota")
  cannot heal within the next few seconds, so it never retries Gemini itself; it
  fails over to the OpenRouter free-model chain immediately.
- A transient failure (short-window rate limit, server error, network error,
  timeout, capacity-unavailable) gets at most one bounded Gemini retry before
  failing over.
- A provider Retry-After is a minimum safe delay, never a value to truncate. If it
  exceeds Research's local wait budget, Research fails over immediately instead of
  sleeping only part of the delay and re-hitting the same provider early.
- A content-safety block never fails over to a different provider: that is a
  safety-gate outcome, not a capacity problem, and silently trying another
  provider would weaken the gate rather than recover from an outage.
- Every other Gemini failure (bad output shape, auth, schema mismatch, ...) fails
  over once to OpenRouter, since that is a different provider and key and may
  simply succeed where Gemini did not.

No AI budget cap is touched, no paid provider is used (OpenRouter's own module
enforces a free-model allowlist), and no quality/safety gate is relaxed.
"""

from __future__ import annotations

import random
import re
import time
from typing import Any

from scripts.provider_failure import classify_provider_failure
from scripts.retry_after_policy import retry_delay_decision
from isco_video_agent.providers.gemini import json_text as gemini_json_text
from isco_video_agent.providers.openrouter import json_text as openrouter_json_text

# Bounded retry budget for one Research provider call. Deliberately small: Research
# runs inside a 5-minute-cron GitHub Actions job shared with other Telegram control
# work. This is a WAIT BUDGET, not permission to truncate provider Retry-After.
MAX_GEMINI_ATTEMPTS_FOR_TRANSIENT_FAILURE = 2
MIN_BACKOFF_SECONDS = 1.0
MAX_RETRY_AFTER_SECONDS = 15.0

# Matches Gemini's own "Please retry in 1.447794966s" style hint.
_RETRY_AFTER_RE = re.compile(r"retry in (\d+(?:\.\d+)?)s", re.IGNORECASE)

# Same daily/session quota vocabulary already used by scripts/provider_failure.py's
# quota_markers, kept local because Research specifically needs the finer split between
# "retrying now cannot help" and "a bounded retry might help".
_QUOTA_MARKERS = (
    "quota_exceeded",
    "quota exceeded",
    "exceeded current quota",
    "daily quota",
    "insufficient quota",
    "free_tier_requests",
)

# Failure classes worth exactly one bounded same-provider retry before failing over.
_RETRYABLE_ON_SAME_PROVIDER = frozenset(
    {"429", "server_error", "network_error", "timeout", "capacity_unavailable"}
)


class ResearchProviderExhausted(RuntimeError):
    """Both Gemini and the OpenRouter fallback failed for one Research provider call."""


def _is_daily_quota_failure(error: BaseException) -> bool:
    detail = str(error).casefold()
    return any(marker in detail for marker in _QUOTA_MARKERS)


def _parsed_retry_after_seconds(error: BaseException) -> float | None:
    match = _RETRY_AFTER_RE.search(str(error))
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


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
    # Every capacity/availability/output-shape failure may try the fallback
    # provider. A content-safety block must not: that is a safety-gate outcome the
    # request itself triggered, not a capacity problem, so it fails closed instead
    # of quietly being retried against a provider with a different safety review.
    return telemetry_result != "content_blocked"


def gemini_research_call_with_fallback(
    api_key: str,
    prompt: str,
    model: str,
    *,
    openrouter_model: str = "openrouter/free",
    openrouter_fallback_models: tuple[str, ...] = ("openai/gpt-oss-20b:free",),
) -> dict[str, Any]:
    """Run one Research JSON provider call with classification, bounded retry, and
    a same-task OpenRouter fallback. Raises ``ResearchProviderExhausted`` (chaining
    the last OpenRouter error) only when both providers have failed."""
    gemini_errors: list[str] = []
    attempt = 0
    while True:
        attempt += 1
        try:
            return gemini_json_text(api_key, prompt, model=model)
        except Exception as exc:  # noqa: BLE001 - classified immediately below
            gemini_errors.append(str(exc)[:300])
            classification = classify_provider_failure("gemini", exc)
            quota = _is_daily_quota_failure(exc)
            can_retry_same_provider = (
                not quota
                and classification.telemetry_result in _RETRYABLE_ON_SAME_PROVIDER
                and attempt < MAX_GEMINI_ATTEMPTS_FOR_TRANSIENT_FAILURE
            )
            if can_retry_same_provider:
                delay = _backoff_seconds(exc, attempt)
                if delay is not None:
                    time.sleep(delay)
                    continue
                print(
                    "Research provider Retry-After exceeds local wait budget: "
                    f"provider=gemini budget={MAX_RETRY_AFTER_SECONDS:g}s action=failover_without_partial_retry"
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
    except Exception as exc:
        raise ResearchProviderExhausted(
            "research_provider_exhausted gemini=["
            + " | ".join(gemini_errors)
            + "] openrouter=[" + str(exc)[:300] + "]"
        ) from exc

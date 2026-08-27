"""Provider reliability for the Crossref scholarly-metadata call site only.

Scope discipline: this module exists because ``_crossref_sources`` (run from
``scripts/telegram_control_simple_ui.py::_research_ready_long_candidates``, used
only for the "long" episode kind's mandatory scholarly research pack) called
``urllib.request.urlopen`` directly with zero retry or backoff. Runs #158/#159
showed this failing with a plain ``HTTP Error 429: Too Many Requests`` that
propagated straight up, aborting the whole research attempt even though the
Gemini/OpenRouter provider chain (see scripts/research_provider_reliability.py)
had already succeeded.

It intentionally does NOT import, patch, or otherwise touch any Planning module
(task_level_planner_router.py, provider_capacity_hardening.py, run_v3_voice.py,
resilient_planner.py, or anything under isco_video_agent's planning/orchestrator
path), and it does NOT change the mandatory "at least two scholarly sources"
approval gate in ``_approve`` - it only protects the HTTP call itself from
failing unnecessarily on a transient condition. It reuses only the one
provider-neutral, side-effect-free classifier already shared across this
codebase (``scripts.provider_failure.classify_provider_failure``) without
modifying it.

Design, matching the same "one retry owner, bounded backoff" shape already
applied to Research's Gemini/OpenRouter call, scoped to a single HTTP call with
no fallback registry (Crossref is the only scholarly-metadata source used
here):

- A transient failure (short-window rate limit, server error, network error,
  timeout, capacity-unavailable) gets a small number of bounded retries -
  backoff honors any provider-supplied ``Retry-After`` header (capped) plus
  jitter.
- Every other failure (404, malformed request, ...) is not retried: retrying a
  deterministic failure would only waste the bounded cron budget shared with
  other Telegram control work.
- When retries are exhausted, this fails closed by raising
  ``CrossrefRetryExhausted`` - it never fabricates sources or relaxes the
  two-source minimum enforced by ``_approve``.
"""

from __future__ import annotations

import random
import time
import urllib.error
import urllib.request
from typing import Callable

from scripts.provider_failure import classify_provider_failure

# Bounded retry budget for one Crossref HTTP call. Deliberately small: this
# runs inside a 5-minute-cron GitHub Actions job shared with other Telegram
# control work, and _research_ready_long_candidates calls this once per
# candidate (up to 3 candidates), so a large budget here compounds quickly.
MAX_ATTEMPTS = 3
MIN_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 15.0

# Failure classes worth a bounded retry against the same endpoint. There is no
# fallback registry for scholarly metadata, so unlike Research's Gemini/
# OpenRouter chain this only ever retries Crossref itself.
_RETRYABLE = frozenset({"429", "server_error", "network_error", "timeout", "capacity_unavailable"})


class CrossrefRetryExhausted(RuntimeError):
    """Crossref kept failing after the bounded retry budget was spent."""


def _retry_after_header_seconds(error: BaseException) -> float | None:
    headers = getattr(error, "headers", None)
    if headers is None:
        return None
    value = headers.get("Retry-After")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _backoff_seconds(error: BaseException, attempt: int) -> float:
    hinted = _retry_after_header_seconds(error)
    base = hinted if hinted is not None else MIN_BACKOFF_SECONDS * attempt
    bounded = min(max(base, MIN_BACKOFF_SECONDS), MAX_BACKOFF_SECONDS)
    return bounded + random.uniform(0.0, 1.0)


def fetch_crossref_response(
    request: urllib.request.Request,
    *,
    timeout: int = 25,
    urlopen: Callable[..., object] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> bytes:
    """Perform one Crossref HTTP call with bounded retry/backoff, then read it.

    Raises ``CrossrefRetryExhausted`` (chaining the last error) only once the
    retry budget for a transient failure is spent, or immediately re-raises a
    non-transient failure without retrying it.
    """
    # Resolved lazily (not as early-bound defaults) so tests can patch
    # urllib.request.urlopen / time.sleep on this module without needing to
    # pass them explicitly on every call.
    if urlopen is None:
        urlopen = urllib.request.urlopen
    if sleep is None:
        sleep = time.sleep
    errors: list[str] = []
    attempt = 0
    retried_at_least_once = False
    while True:
        attempt += 1
        try:
            with urlopen(request, timeout=timeout) as response:
                return response.read()
        except Exception as exc:  # noqa: BLE001 - classified immediately below
            errors.append(str(exc)[:300])
            classification = classify_provider_failure("crossref", exc)
            can_retry = (
                classification.telemetry_result in _RETRYABLE
                and attempt < MAX_ATTEMPTS
            )
            if can_retry:
                retried_at_least_once = True
                sleep(_backoff_seconds(exc, attempt))
                continue
            if not retried_at_least_once:
                # Never retried - either not a transient class, or the retry
                # budget is zero. Surface the original error unwrapped.
                raise
            raise CrossrefRetryExhausted(
                "crossref_retry_exhausted errors=[" + " | ".join(errors) + "]"
            ) from exc

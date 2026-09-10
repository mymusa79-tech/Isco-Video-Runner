from __future__ import annotations

"""Run-scoped cross-capability provider-health evidence.

This registry owns *availability state*, not provider policy.  It keeps hard failures
sticky for the current Vision scope, keeps short-window rate evidence time-bounded, and
treats 5xx/timeout/network failures as recoverable transient circuit events instead of
permanent provider death.

The compatibility surface remains ``publish_provider_unavailable`` /
``provider_unavailable`` so older Run181/Visual-V1 callers continue to work.  New code
may use ``publish_provider_failure`` and ``record_provider_success`` directly.

Important invariants:
- evidence is scoped by provider + model + quota_domain;
- provider-wide preflight BLOCK rows remain hard and wildcard-scoped;
- STRUCTURAL failures are observable but never poison the whole provider;
- TRANSIENT failures get at most two half-open recovery probes after the initial failed
  call in one Vision scope, with bounded candidate-level backoff and no hidden sleep;
- RATE_LIMITED evidence is never bypassed before its retry time and gets the same bounded
  half-open ceiling;
- a successful half-open probe closes the consecutive-failure family on the next lookup
  (or immediately through ``record_provider_success`` when a caller can report success);
- a new Vision scope clears all evidence, counters and pending probes.
"""

import json
import os
import re
import time
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path


FAILURE_HARD = "hard"
FAILURE_MODEL_HARD = "model_hard"
FAILURE_RATE_LIMITED = "rate_limited"
FAILURE_TRANSIENT = "transient"
FAILURE_STRUCTURAL = "structural"

MAX_TRANSIENT_HALF_OPEN_PROBES = 2
DEFAULT_RATE_LIMIT_RETRY_SECONDS = 60.0
# Transient backoff is expressed primarily as bounded provider *reads* so the pipeline
# never sleeps inside health lookup.  The first read is normally the mesh diagnostic;
# later reads correspond to later candidates.  Repeated failures increase the number of
# skipped candidates before the next half-open probe.
MAX_TRANSIENT_BLOCK_READS = 3


@dataclass(frozen=True)
class ProviderHealthEvidence:
    provider: str
    model: str
    quota_domain: str
    status: str
    reason: str
    source: str
    failure_class: str = FAILURE_HARD
    observed_at: float = 0.0
    retry_at: float | None = None
    failure_count: int = 1
    block_reads_remaining: int = 0


_EVIDENCE: ContextVar[tuple[ProviderHealthEvidence, ...]] = ContextVar(
    "isco_provider_health_evidence",
    default=(),
)
_LOADED_PREFLIGHT: ContextVar[bool] = ContextVar(
    "isco_provider_health_preflight_loaded",
    default=False,
)
_BOUND_VISION_SCOPE: ContextVar[object | None] = ContextVar(
    "isco_provider_health_bound_vision_scope",
    default=None,
)
_CONSECUTIVE_FAILURES: ContextVar[dict[tuple[str, str, str], int]] = ContextVar(
    "isco_provider_health_consecutive_failures",
    default={},
)
_PENDING_HALF_OPEN: ContextVar[set[tuple[str, str, str]]] = ContextVar(
    "isco_provider_health_pending_half_open",
    default=set(),
)


def _normalized_key(
    provider: str,
    model: str = "*",
    quota_domain: str = "*",
) -> tuple[str, str, str]:
    return (
        str(provider or "").strip().lower(),
        str(model or "*").strip(),
        str(quota_domain or "*").strip().lower(),
    )


def _set_counter(key: tuple[str, str, str], value: int) -> None:
    current = dict(_CONSECUTIVE_FAILURES.get())
    if value > 0:
        current[key] = int(value)
    else:
        current.pop(key, None)
    _CONSECUTIVE_FAILURES.set(current)


def _set_pending(key: tuple[str, str, str], pending: bool) -> None:
    current = set(_PENDING_HALF_OPEN.get())
    if pending:
        current.add(key)
    else:
        current.discard(key)
    _PENDING_HALF_OPEN.set(current)


def _remove_exact_evidence(key: tuple[str, str, str]) -> None:
    _EVIDENCE.set(
        tuple(
            entry
            for entry in _EVIDENCE.get()
            if (entry.provider, entry.model, entry.quota_domain) != key
        )
    )


def reset_provider_health() -> None:
    _EVIDENCE.set(())
    _LOADED_PREFLIGHT.set(False)
    _BOUND_VISION_SCOPE.set(None)
    _CONSECUTIVE_FAILURES.set({})
    _PENDING_HALF_OPEN.set(set())


def bind_provider_health_to_vision_scope(scope: object) -> bool:
    """Bind evidence to one existing Vision circuit scope.

    Returns True only when a new scope was observed. Callers can use that signal to
    reset other run-scoped provider certification caches at exactly the same boundary.
    """
    if scope is None:
        raise ValueError("provider health requires a concrete Vision scope")
    if _BOUND_VISION_SCOPE.get() is scope:
        return False
    _EVIDENCE.set(())
    _LOADED_PREFLIGHT.set(False)
    _BOUND_VISION_SCOPE.set(scope)
    _CONSECUTIVE_FAILURES.set({})
    _PENDING_HALF_OPEN.set(set())
    return True


def _infer_failure_class(reason: object, *, source: str) -> str:
    text = str(reason or "").casefold()
    normalized_source = str(source or "").strip().lower()

    # Explicit preflight BLOCK is authoritative for the current run.  Do not reinterpret
    # it as transient merely because the detail happens to mention an HTTP status.
    if normalized_source == "provider_preflight":
        return FAILURE_HARD

    if any(
        marker in text
        for marker in (
            "model not found",
            "model_not_found",
            "unsupported model",
            "does not support vision",
            "vision is not supported",
            "unknown model",
        )
    ):
        return FAILURE_MODEL_HARD

    if any(
        marker in text
        for marker in (
            "401",
            "403",
            "unauthorized",
            "authentication",
            "invalid api key",
            "invalid_api_key",
            "permission denied",
            "forbidden",
            "billing",
            "insufficient credit",
            "insufficient_credit",
            "spend capacity exhausted",
            "payment required",
            "http_402",
        )
    ):
        return FAILURE_HARD

    if any(
        marker in text
        for marker in (
            "requests per day",
            "tokens per day",
            "daily limit",
            "daily quota",
            " rpd",
            " tpd",
        )
    ):
        # A known daily ceiling is not a short-window throttle.  Keep it run-hard; the
        # next production run gets a fresh scope instead of burning retries today.
        return FAILURE_HARD

    if any(
        marker in text
        for marker in (
            "429",
            "rate limit",
            "rate_limit",
            "resource_exhausted",
            "quota exceeded",
            "quota exhausted",
        )
    ):
        return FAILURE_RATE_LIMITED

    if any(
        marker in text
        for marker in (
            "structural_invalid",
            "schema",
            "not valid json",
            "invalid json",
            "response has no choice",
            "response message missing",
            "response content is not text",
        )
    ):
        return FAILURE_STRUCTURAL

    if (
        any(marker in text for marker in ("http_408", "http_500", "http_502", "http_503", "http_504"))
        or any(
            marker in text
            for marker in (
                "provider_transient",
                "timeout",
                "timed out",
                "connection",
                "network",
                "transport failure",
                "service unavailable",
                "temporarily unavailable",
                "currently over capacity",
                "over capacity",
            )
        )
    ):
        return FAILURE_TRANSIENT

    # Existing callers historically used this API only for explicit unavailability.
    # Unknown failure shapes therefore stay fail-closed rather than being guessed
    # transient.
    return FAILURE_HARD


def _retry_after_from_reason(reason: object) -> float | None:
    text = str(reason or "")
    patterns = (
        r"retry[- ]after[=: ]+([0-9.]+)",
        r"try again in\s+([0-9.]+)\s*s",
        r"reset(?:s|_seconds)?[=: ]+([0-9.]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if not match:
            continue
        try:
            value = float(match.group(1))
        except (TypeError, ValueError):
            continue
        if value >= 0:
            return min(value, 3600.0)
    return None


def publish_provider_failure(
    provider: str,
    *,
    model: str = "*",
    quota_domain: str = "*",
    reason: str,
    source: str,
    failure_class: str,
    retry_after_seconds: float | None = None,
) -> ProviderHealthEvidence:
    """Publish classified provider evidence without weakening fail-closed behavior."""
    key = _normalized_key(provider, model, quota_domain)
    normalized_class = str(failure_class or FAILURE_HARD).strip().lower()
    if normalized_class not in {
        FAILURE_HARD,
        FAILURE_MODEL_HARD,
        FAILURE_RATE_LIMITED,
        FAILURE_TRANSIENT,
        FAILURE_STRUCTURAL,
    }:
        normalized_class = FAILURE_HARD

    pending = key in _PENDING_HALF_OPEN.get()
    previous_count = int(_CONSECUTIVE_FAILURES.get().get(key, 0))
    if normalized_class == FAILURE_STRUCTURAL:
        # Structural output belongs to a model/attempt, not provider availability.
        count = previous_count
    else:
        count = previous_count + 1 if (pending or previous_count) else 1
    _set_pending(key, False)
    if normalized_class != FAILURE_STRUCTURAL:
        _set_counter(key, count)

    now = time.monotonic()
    retry_at: float | None = None
    block_reads = 0
    status = "unavailable"

    if normalized_class == FAILURE_RATE_LIMITED:
        delay = retry_after_seconds
        if delay is None:
            delay = _retry_after_from_reason(reason)
        if delay is None:
            delay = DEFAULT_RATE_LIMIT_RETRY_SECONDS
        retry_at = now + max(0.0, float(delay))
    elif normalized_class == FAILURE_TRANSIENT:
        # Initial failure + two half-open probes maximum in one Vision scope.
        # No hidden sleep: increasing read blocks naturally spaces probes across later
        # visual candidates while the provider mesh remains bounded.
        block_reads = min(MAX_TRANSIENT_BLOCK_READS, max(1, int(count)))
        if count > 1 + MAX_TRANSIENT_HALF_OPEN_PROBES:
            block_reads = MAX_TRANSIENT_BLOCK_READS
    elif normalized_class == FAILURE_STRUCTURAL:
        status = "degraded"

    item = ProviderHealthEvidence(
        provider=key[0],
        model=key[1],
        quota_domain=key[2],
        status=status,
        reason=str(reason or "provider unavailable").replace("\n", " ").strip()[:300],
        source=str(source or "runtime").strip()[:120],
        failure_class=normalized_class,
        observed_at=now,
        retry_at=retry_at,
        failure_count=max(1, count),
        block_reads_remaining=block_reads,
    )

    current = [
        entry
        for entry in _EVIDENCE.get()
        if (entry.provider, entry.model, entry.quota_domain) != key
    ]
    current.append(item)
    _EVIDENCE.set(tuple(current))
    return item


def publish_provider_unavailable(
    provider: str,
    *,
    model: str = "*",
    quota_domain: str = "*",
    reason: str,
    source: str,
) -> None:
    """Compatibility publisher with stable failure taxonomy inferred from evidence."""
    publish_provider_failure(
        provider,
        model=model,
        quota_domain=quota_domain,
        reason=reason,
        source=source,
        failure_class=_infer_failure_class(reason, source=source),
    )


def _find_matching_entry(
    provider: str,
    *,
    model: str,
    quota_domain: str,
    blocking_only: bool = False,
) -> tuple[int, ProviderHealthEvidence] | None:
    provider = str(provider).strip().lower()
    model = str(model or "*").strip()
    quota_domain = str(quota_domain or "*").strip().lower()
    entries = _EVIDENCE.get()
    matches: list[tuple[int, ProviderHealthEvidence]] = []
    for index in range(len(entries) - 1, -1, -1):
        entry = entries[index]
        if entry.provider != provider:
            continue
        if entry.model not in {"*", model}:
            continue
        if entry.quota_domain not in {"*", quota_domain}:
            continue
        if blocking_only and entry.status != "unavailable":
            continue
        matches.append((index, entry))

    if not matches:
        return None

    # A hard provider-wide/preflight block always dominates a later exact transient
    # row; exact success or degraded schema evidence must never bypass the authoritative
    # hard block.
    for item in matches:
        entry = item[1]
        if entry.status == "unavailable" and entry.failure_class in {
            FAILURE_HARD,
            FAILURE_MODEL_HARD,
        }:
            return item
    return matches[0]


def _matching_pending_key(
    provider: str,
    *,
    model: str,
    quota_domain: str,
) -> tuple[str, str, str] | None:
    provider = str(provider).strip().lower()
    model = str(model or "*").strip()
    quota_domain = str(quota_domain or "*").strip().lower()
    for key in _PENDING_HALF_OPEN.get():
        if key[0] != provider:
            continue
        if key[1] not in {"*", model}:
            continue
        if key[2] not in {"*", quota_domain}:
            continue
        return key
    return None


def _replace_evidence(index: int, entry: ProviderHealthEvidence) -> None:
    current = list(_EVIDENCE.get())
    if index < 0 or index >= len(current):
        return
    current[index] = entry
    _EVIDENCE.set(tuple(current))


def _active_legacy_state():
    """Return the current Vision circuit without ever creating one.

    ``legacy._state()`` intentionally creates a fallback state for direct diagnostic
    calls. Provider-health lookup must not do that: a health read outside an active
    Vision scope would otherwise leak Gemini/OpenRouter circuit state into later calls
    or tests. Production scopes remain owned by ``vision_provider_circuit_scope``.
    """
    try:
        from scripts import vision_provider_reliability as legacy
        return legacy._VISION_CIRCUIT.get()
    except Exception:
        return None


def _clear_legacy_circuit_if_recoverable(
    provider: str,
    *,
    failure_class: str,
) -> None:
    """Release only a legacy local circuit that is demonstrably non-hard.

    Run181's legacy Gemini/OpenRouter state predates classified health.  A transient or
    rate half-open would otherwise be admitted here and still be blocked by the older
    ``*_open`` boolean.  Health reads are observational outside an active Vision scope
    and must never create a legacy circuit as a side effect.
    """
    normalized = str(provider).strip().lower()
    if normalized not in {"gemini", "openrouter"}:
        return
    if failure_class not in {FAILURE_TRANSIENT, FAILURE_RATE_LIMITED}:
        return
    state = _active_legacy_state()
    if state is None:
        return
    open_attr = f"{normalized}_open"
    reason_attr = f"{normalized}_reason"
    if not bool(getattr(state, open_attr, False)):
        return
    reason = str(getattr(state, reason_attr, "") or "")
    inferred = _infer_failure_class(reason, source="legacy_vision_state")
    if inferred not in {FAILURE_TRANSIENT, FAILURE_RATE_LIMITED}:
        return
    setattr(state, open_attr, False)
    setattr(state, reason_attr, None)


def _recover_unpublished_legacy_gemini_transient(
    provider: str,
    *,
    model: str,
    quota_domain: str,
) -> bool:
    """Bound Run181's old local-only Gemini transient circuit.

    Run181 publishes shared Gemini health for quota/rate evidence, but a Gemini 5xx or
    transport failure only opens its legacy local circuit.  On the next candidate this
    helper gives at most two half-open probes; hard/auth/internal states are untouched.
    Health lookup outside an active Vision scope is strictly side-effect free.
    """
    if str(provider).strip().lower() != "gemini":
        return False
    key = _normalized_key(provider, model, quota_domain)
    if _find_matching_entry(
        provider,
        model=model,
        quota_domain=quota_domain,
        blocking_only=True,
    ) is not None:
        return False
    state = _active_legacy_state()
    if state is None:
        return False
    if not bool(getattr(state, "gemini_open", False)):
        # A previously granted probe survived without republishing a failure: success.
        if key in _PENDING_HALF_OPEN.get():
            _set_pending(key, False)
            _set_counter(key, 0)
        return False

    reason = str(getattr(state, "gemini_reason", "") or "")
    failure_class = _infer_failure_class(reason, source="legacy_vision_state")
    if failure_class != FAILURE_TRANSIENT:
        return False

    # If a previous half-open probe ended by reopening the legacy circuit, that pending
    # lease failed. Close the lease before counting the next failure; otherwise the
    # generic no-evidence path could mistake a failed probe for success and reset the
    # counter, enabling an unbounded retry family.
    _set_pending(key, False)
    count = int(_CONSECUTIVE_FAILURES.get().get(key, 0)) + 1
    _set_counter(key, count)
    if count > MAX_TRANSIENT_HALF_OPEN_PROBES:
        return False

    state.gemini_open = False
    state.gemini_reason = None
    _set_pending(key, True)
    print(
        "Provider Health: Gemini transient legacy circuit admitted one bounded half-open "
        f"probe={count}/{MAX_TRANSIENT_HALF_OPEN_PROBES} model={model}"
    )
    return True


def provider_unavailable(
    provider: str,
    *,
    model: str,
    quota_domain: str,
) -> ProviderHealthEvidence | None:
    provider = str(provider).strip().lower()
    model = str(model or "*").strip()
    quota_domain = str(quota_domain or "*").strip().lower()

    matched = _find_matching_entry(
        provider,
        model=model,
        quota_domain=quota_domain,
        blocking_only=True,
    )
    if matched is None:
        if provider == "gemini":
            recovered = _recover_unpublished_legacy_gemini_transient(
                provider,
                model=model,
                quota_domain=quota_domain,
            )
            if recovered:
                return None
        pending_key = _matching_pending_key(
            provider,
            model=model,
            quota_domain=quota_domain,
        )
        if pending_key is not None:
            # No failure was republished after the previously granted probe, therefore
            # the prior probe succeeded and the consecutive-failure family is closed.
            _set_pending(pending_key, False)
            _set_counter(pending_key, 0)
        return None

    index, entry = matched
    if entry.status != "unavailable":
        # Structural/model-output evidence is observable but non-blocking.
        return None

    key = (entry.provider, entry.model, entry.quota_domain)

    # Provider-wide preflight/hard evidence remains sticky.  Runtime wildcard evidence
    # can still be transient (Run181 historically publishes OpenRouter runtime failures
    # with wildcards), so wildcard shape alone must not make a 5xx permanent.
    if entry.failure_class in {FAILURE_HARD, FAILURE_MODEL_HARD}:
        return entry

    if entry.failure_class == FAILURE_RATE_LIMITED:
        # A short-window throttle may recover, but it still receives only the same two
        # half-open recovery probes as other transient provider failures. Repeated 429s
        # therefore cannot create an unbounded wait/retry loop across visual candidates.
        if entry.failure_count >= 1 + MAX_TRANSIENT_HALF_OPEN_PROBES:
            return entry
        if entry.retry_at is None or time.monotonic() < entry.retry_at:
            return entry
        _remove_exact_evidence(key)
        _set_pending(key, True)
        _clear_legacy_circuit_if_recoverable(
            provider,
            failure_class=entry.failure_class,
        )
        return None

    if entry.failure_class == FAILURE_TRANSIENT:
        # After the initial failed call, admit at most two half-open recovery probes.
        # A mesh diagnostic read consumes one block read; repeated failures increase the
        # spacing between later probes and finally leave the route open for the run.
        if entry.failure_count >= 1 + MAX_TRANSIENT_HALF_OPEN_PROBES:
            return entry
        if entry.block_reads_remaining > 0:
            _replace_evidence(
                index,
                replace(
                    entry,
                    block_reads_remaining=entry.block_reads_remaining - 1,
                ),
            )
            return entry
        _remove_exact_evidence(key)
        _set_pending(key, True)
        _clear_legacy_circuit_if_recoverable(
            provider,
            failure_class=entry.failure_class,
        )
        return None

    return entry


def record_provider_success(
    provider: str,
    *,
    model: str,
    quota_domain: str,
) -> None:
    """Close exact recoverable health state after a successful provider call."""
    key = _normalized_key(provider, model, quota_domain)
    _remove_exact_evidence(key)
    _set_pending(key, False)
    _set_counter(key, 0)


def _default_preflight_path() -> Path | None:
    explicit = str(os.environ.get("ISCO_PROVIDER_PREFLIGHT_PATH") or "").strip()
    if explicit:
        return Path(explicit)
    runner_temp = str(os.environ.get("RUNNER_TEMP") or "").strip()
    return Path(runner_temp) / "provider-preflight.json" if runner_temp else None


def load_preflight_provider_health(path: str | Path | None = None) -> None:
    """Seed provider-wide hard evidence from the zero-inference provider preflight.

    Only explicit status=block rows are imported.  A ``pass`` with dynamic/unobservable
    inference capacity never becomes a false healthy guarantee.
    """
    if _LOADED_PREFLIGHT.get():
        return
    target = Path(path) if path is not None else _default_preflight_path()
    if target is None or not target.is_file():
        _LOADED_PREFLIGHT.set(True)
        return
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        _LOADED_PREFLIGHT.set(True)
        return
    checks = payload.get("checks") if isinstance(payload, dict) else None
    if isinstance(checks, list):
        for row in checks:
            if not isinstance(row, dict) or str(row.get("status") or "").lower() != "block":
                continue
            provider = str(row.get("provider") or "").strip().lower()
            if not provider:
                continue
            publish_provider_failure(
                provider,
                model="*",
                quota_domain="*",
                reason=str(row.get("detail") or "preflight blocked provider"),
                source="provider_preflight",
                failure_class=FAILURE_HARD,
            )
    _LOADED_PREFLIGHT.set(True)


def snapshot_provider_health() -> list[dict[str, object]]:
    return [
        {
            "provider": entry.provider,
            "model": entry.model,
            "quota_domain": entry.quota_domain,
            "status": entry.status,
            "reason": entry.reason,
            "source": entry.source,
            "failure_class": entry.failure_class,
            "failure_count": entry.failure_count,
            "block_reads_remaining": entry.block_reads_remaining,
            "retry_at": entry.retry_at,
        }
        for entry in _EVIDENCE.get()
    ]

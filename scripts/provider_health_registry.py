from __future__ import annotations

"""Run-scoped cross-capability provider-health evidence.

A provider failure discovered by Planning/Text Audit must not be forgotten before
Vision reaches the same network model/quota domain. Evidence is deliberately scoped by
provider + model + quota_domain so one model/capability cannot poison unrelated ones.
Provider-wide preflight blocks (for example exhausted OpenRouter spend capacity) use
wildcards and therefore apply to every later capability in the same production run.

The lifecycle is bound to the existing run-scoped Vision circuit owner. A new Vision
scope clears all prior evidence and preflight-load state; subsequent candidate reviews
inside that same scope share evidence. This prevents both cross-run contamination and
cross-test contamination without inventing a second production lifecycle owner.

Run233 hardening: the public compatibility API is unchanged, but evidence is no longer
implicitly equivalent to permanent run death. The canonical provider_failure classifier
now separates hard/run-scoped conditions from transient transport/capacity conditions
and request-scoped structural failures. Transients receive a small bounded backoff and
are automatically probe-eligible afterwards; structural failures are retained only as
safe observations. This prevents a 5xx/timeout on one candidate from poisoning every
later candidate while keeping auth, verified quota/rate and deterministic capability
blocks fail-closed.
"""

import json
import os
import time
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from scripts.provider_failure import classify_provider_failure


_HARD_EVIDENCE_SOURCES = frozenset(
    {
        "provider_preflight",
        "vision_catalog_preflight",
        "vision_static_readiness",
    }
)
_TRANSIENT_RESULTS = frozenset({"server_error", "timeout", "network_error"})
_TRANSIENT_BACKOFF_BASE_SECONDS = 0.5
_TRANSIENT_BACKOFF_MAX_SECONDS = 2.0


@dataclass(frozen=True)
class ProviderHealthEvidence:
    provider: str
    model: str
    quota_domain: str
    status: str
    reason: str
    source: str
    failure_class: str = "hard"
    observed_at_monotonic: float = 0.0
    retry_at_monotonic: float | None = None
    failure_count: int = 1


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


def reset_provider_health() -> None:
    _EVIDENCE.set(())
    _LOADED_PREFLIGHT.set(False)
    _BOUND_VISION_SCOPE.set(None)


def _same_route(
    entry: ProviderHealthEvidence,
    *,
    provider: str,
    model: str,
    quota_domain: str,
) -> bool:
    return (
        entry.provider == provider
        and entry.model == model
        and entry.quota_domain == quota_domain
    )


def _matches_route(
    entry: ProviderHealthEvidence,
    *,
    provider: str,
    model: str,
    quota_domain: str,
) -> bool:
    if entry.provider != provider:
        return False
    if entry.model not in {"*", model}:
        return False
    return entry.quota_domain in {"*", quota_domain}


def _request_scoped_legacy_reason(provider: str, reason: object) -> bool:
    """Return True only for an old private circuit that is safe to re-probe.

    Run181 still writes Gemini/OpenRouter booleans on its legacy circuit state. Until
    that compatibility state disappears, this registry is the single lifecycle owner:
    a later candidate may release only explicit transient/structural reasons. Shared
    health reasons are never cleared here because their exact route evidence remains
    authoritative in this registry.
    """
    text = str(reason or "").strip()
    lowered = text.casefold()
    if not text or lowered.startswith("shared_health:"):
        return False
    if "structural_invalid" in lowered:
        return True
    decision = classify_provider_failure(provider, text)
    if decision.open_circuit:
        return False
    return (
        "provider_transient" in lowered
        or decision.telemetry_result in _TRANSIENT_RESULTS
        or decision.telemetry_result in {"invalid_json", "generation_error"}
    )


def _release_request_scoped_legacy_circuits(scope: object) -> None:
    # The legacy object is deliberately duck-typed so provider_health_registry does not
    # import the Vision module and create a circular dependency.
    for provider, open_name, reason_name in (
        ("gemini", "gemini_open", "gemini_reason"),
        ("openrouter", "openrouter_open", "openrouter_reason"),
    ):
        if not bool(getattr(scope, open_name, False)):
            continue
        reason = str(getattr(scope, reason_name, "") or "")
        if not _request_scoped_legacy_reason(provider, reason):
            continue
        setattr(scope, open_name, False)
        setattr(scope, reason_name, "")
        print(
            "Provider Health: released request-scoped Vision circuit for bounded re-probe; "
            f"provider={provider} reason={reason[:180]}"
        )


def bind_provider_health_to_vision_scope(scope: object) -> bool:
    """Bind evidence to one existing Vision circuit scope.

    Returns True only when a new scope was observed. Callers can use that signal to
    reset other run-scoped provider certification caches at exactly the same boundary.
    Request-scoped legacy Gemini/OpenRouter circuit flags are also released here before
    each later candidate, while hard/shared-health flags remain untouched.
    """
    if scope is None:
        raise ValueError("provider health requires a concrete Vision scope")
    if _BOUND_VISION_SCOPE.get() is scope:
        _release_request_scoped_legacy_circuits(scope)
        return False
    _EVIDENCE.set(())
    _LOADED_PREFLIGHT.set(False)
    _BOUND_VISION_SCOPE.set(scope)
    return True


def _lifecycle_for(
    provider: str,
    *,
    reason: str,
    source: str,
) -> tuple[str, str]:
    lowered = reason.casefold()
    decision = classify_provider_failure(provider, reason)

    # These sources are zero-inference/deterministic readiness evidence. They stay hard
    # even if their human-readable detail has no HTTP marker for the generic classifier.
    if source in _HARD_EVIDENCE_SOURCES:
        return "unavailable", "hard"
    if "auth_config" in lowered or " capacity " in f" {lowered} ":
        return "unavailable", "hard"
    if decision.telemetry_result == "429":
        return "unavailable", "rate_limited"
    if decision.open_circuit:
        return "unavailable", "hard"
    if "structural_invalid" in lowered:
        return "observation", "structural"
    if "provider_transient" in lowered or decision.telemetry_result in _TRANSIENT_RESULTS:
        return "transient", "transient"
    return "observation", "request_scoped"


def publish_provider_unavailable(
    provider: str,
    *,
    model: str = "*",
    quota_domain: str = "*",
    reason: str,
    source: str,
) -> None:
    """Publish provider health while preserving the historical call surface.

    Despite the compatibility name, transient and request-scoped failures are no longer
    promoted to permanent `unavailable` evidence. Hard/rate evidence remains sticky for
    the current Vision scope. Transient evidence carries bounded exponential backoff;
    provider_unavailable() waits only that bounded interval and then admits a half-open
    probe on the next logical candidate.
    """
    normalized_provider = str(provider).strip().lower()
    normalized_model = str(model or "*").strip()
    normalized_quota = str(quota_domain or "*").strip().lower()
    normalized_reason = str(reason or "provider unavailable").replace("\n", " ").strip()[:300]
    normalized_source = str(source or "runtime").strip()[:120]
    status, failure_class = _lifecycle_for(
        normalized_provider,
        reason=normalized_reason,
        source=normalized_source,
    )

    current = list(_EVIDENCE.get())
    previous = next(
        (
            entry
            for entry in reversed(current)
            if _same_route(
                entry,
                provider=normalized_provider,
                model=normalized_model,
                quota_domain=normalized_quota,
            )
        ),
        None,
    )
    failure_count = 1
    if status == "transient" and previous is not None and previous.status == "transient":
        failure_count = max(1, int(previous.failure_count)) + 1

    observed_at = time.monotonic()
    retry_at: float | None = None
    if status == "transient":
        cooldown = min(
            _TRANSIENT_BACKOFF_BASE_SECONDS * (2 ** (failure_count - 1)),
            _TRANSIENT_BACKOFF_MAX_SECONDS,
        )
        retry_at = observed_at + cooldown

    item = ProviderHealthEvidence(
        provider=normalized_provider,
        model=normalized_model,
        quota_domain=normalized_quota,
        status=status,
        reason=normalized_reason,
        source=normalized_source,
        failure_class=failure_class,
        observed_at_monotonic=observed_at,
        retry_at_monotonic=retry_at,
        failure_count=failure_count,
    )
    current = [
        entry
        for entry in current
        if not _same_route(
            entry,
            provider=item.provider,
            model=item.model,
            quota_domain=item.quota_domain,
        )
    ]
    current.append(item)
    _EVIDENCE.set(tuple(current))


def provider_unavailable(
    provider: str,
    *,
    model: str,
    quota_domain: str,
) -> ProviderHealthEvidence | None:
    provider = str(provider).strip().lower()
    model = str(model or "*").strip()
    quota_domain = str(quota_domain or "*").strip().lower()
    for entry in reversed(_EVIDENCE.get()):
        if not _matches_route(
            entry,
            provider=provider,
            model=model,
            quota_domain=quota_domain,
        ):
            continue
        if entry.status == "unavailable":
            return entry
        if entry.status != "transient":
            continue

        # A transient is pacing evidence, not run-death evidence. Wait only the bounded
        # registry-owned interval, then return None so the caller's existing attempt cap
        # controls exactly one later logical probe. Repeated failure republishes a longer
        # (capped) interval; success naturally leaves the expired observation non-blocking.
        retry_at = entry.retry_at_monotonic
        if retry_at is not None:
            remaining = max(0.0, retry_at - time.monotonic())
            if remaining:
                time.sleep(min(remaining, _TRANSIENT_BACKOFF_MAX_SECONDS))
        return None
    return None


def _default_preflight_path() -> Path | None:
    explicit = str(os.environ.get("ISCO_PROVIDER_PREFLIGHT_PATH") or "").strip()
    if explicit:
        return Path(explicit)
    runner_temp = str(os.environ.get("RUNNER_TEMP") or "").strip()
    return Path(runner_temp) / "provider-preflight.json" if runner_temp else None


def load_preflight_provider_health(path: str | Path | None = None) -> None:
    """Seed provider-wide hard evidence from the already-produced zero-inference preflight.

    Only explicit status=block rows are imported. `pass` with dynamic/unobservable quota
    never becomes a false healthy guarantee. This makes Run #181's known OpenRouter
    spend-cap exhaustion visible to Vision without changing provider-preflight policy.
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
            publish_provider_unavailable(
                provider,
                model="*",
                quota_domain="*",
                reason=str(row.get("detail") or "preflight blocked provider"),
                source="provider_preflight",
            )
    _LOADED_PREFLIGHT.set(True)


def snapshot_provider_health() -> list[dict[str, str]]:
    """Backward-compatible compact snapshot used by existing callers/tests."""
    return [
        {
            "provider": entry.provider,
            "model": entry.model,
            "quota_domain": entry.quota_domain,
            "status": entry.status,
            "reason": entry.reason,
            "source": entry.source,
        }
        for entry in _EVIDENCE.get()
    ]


def snapshot_provider_health_details() -> list[dict[str, object]]:
    """Detailed safe lifecycle telemetry for diagnostics and regression evidence."""
    now = time.monotonic()
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
            "retry_after_seconds": (
                max(0.0, entry.retry_at_monotonic - now)
                if entry.retry_at_monotonic is not None
                else None
            ),
        }
        for entry in _EVIDENCE.get()
    ]

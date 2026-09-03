from __future__ import annotations

"""Run-scoped cross-capability provider-health evidence.

A provider failure discovered by Planning/Text Audit must not be forgotten before
Vision reaches the same network model/quota domain. Evidence is deliberately scoped by
provider + model + quota_domain so one model/capability cannot poison unrelated ones.
Provider-wide preflight blocks (for example exhausted OpenRouter spend capacity) use
wildcards and therefore apply to every later capability in the same process.
"""

import json
import os
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProviderHealthEvidence:
    provider: str
    model: str
    quota_domain: str
    status: str
    reason: str
    source: str


_EVIDENCE: ContextVar[tuple[ProviderHealthEvidence, ...]] = ContextVar(
    "isco_provider_health_evidence",
    default=(),
)
_LOADED_PREFLIGHT: ContextVar[bool] = ContextVar(
    "isco_provider_health_preflight_loaded",
    default=False,
)


def reset_provider_health() -> None:
    _EVIDENCE.set(())
    _LOADED_PREFLIGHT.set(False)


def publish_provider_unavailable(
    provider: str,
    *,
    model: str = "*",
    quota_domain: str = "*",
    reason: str,
    source: str,
) -> None:
    item = ProviderHealthEvidence(
        provider=str(provider).strip().lower(),
        model=str(model or "*").strip(),
        quota_domain=str(quota_domain or "*").strip().lower(),
        status="unavailable",
        reason=str(reason or "provider unavailable").replace("\n", " ").strip()[:300],
        source=str(source or "runtime").strip()[:120],
    )
    current = list(_EVIDENCE.get())
    current = [
        entry
        for entry in current
        if not (
            entry.provider == item.provider
            and entry.model == item.model
            and entry.quota_domain == item.quota_domain
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
        if entry.provider != provider or entry.status != "unavailable":
            continue
        if entry.model not in {"*", model}:
            continue
        if entry.quota_domain not in {"*", quota_domain}:
            continue
        return entry
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

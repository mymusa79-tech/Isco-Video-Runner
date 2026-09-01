from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import isco_video_agent.resilient_planner as staged

from scripts.short_cinematic_director import MAX_SHORT_SHOTS


# Observable request-count providers should be certified against the request topology,
# not merely `remaining > 0`. Long-form visual recovery can need a primary + alternate
# search per section. A standalone Short uses one already-selected core asset, then at
# most primary + alternate retrievals for each additional beat. Use the larger topology
# so one shared preflight reserve covers both formats without a magic absolute number.
MAX_LONGFORM_SECTIONS = max(staged._SECTION_COUNTS.values())
LONGFORM_MEDIA_SEARCH_RESERVE = MAX_LONGFORM_SECTIONS * 2
SHORT_MEDIA_SEARCH_RESERVE = 1 + ((MAX_SHORT_SHOTS - 1) * 2)
MEDIA_SEARCH_REQUEST_RESERVE = max(
    LONGFORM_MEDIA_SEARCH_RESERVE,
    SHORT_MEDIA_SEARCH_RESERVE,
)


@dataclass(frozen=True)
class MediaCapacityMargin:
    provider: str
    status: str
    remaining: int | None
    required_reserve: int
    hard_dependency: bool


def _provider_preflight_path() -> Path | None:
    explicit = str(os.environ.get("ISCO_PROVIDER_PREFLIGHT_PATH") or "").strip()
    if explicit:
        return Path(explicit)
    temp = str(os.environ.get("RUNNER_TEMP") or "").strip()
    return Path(temp) / "provider-preflight.json" if temp else None


def _load_checks(path: Path) -> dict[str, dict]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("provider capacity margin audit requires valid provider-preflight.json") from exc
    checks = payload.get("checks")
    if not isinstance(checks, list):
        raise RuntimeError("provider capacity margin audit requires provider checks")
    result: dict[str, dict] = {}
    for item in checks:
        if not isinstance(item, dict):
            continue
        provider = str(item.get("provider") or "").strip().lower()
        if provider:
            result[provider] = item
    return result


def _remaining_int(check: dict) -> int | None:
    value = check.get("capacity_remaining")
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value >= 0:
        return int(value)
    return None


def audit_media_capacity_margin(path: Path | None = None) -> tuple[MediaCapacityMargin, ...]:
    target = path or _provider_preflight_path()
    if target is None or not target.is_file():
        # Unit/offline callers do not fabricate provider state. Canonical V4 always
        # materializes this file immediately before planning-envelope certification.
        return ()
    checks = _load_checks(target)
    results: list[MediaCapacityMargin] = []

    for provider, hard_dependency in (("pexels", True), ("pixabay", False)):
        check = checks.get(provider, {})
        if str(check.get("status") or "").lower() != "pass":
            results.append(
                MediaCapacityMargin(
                    provider=provider,
                    status="provider_blocked",
                    remaining=_remaining_int(check),
                    required_reserve=MEDIA_SEARCH_REQUEST_RESERVE,
                    hard_dependency=hard_dependency,
                )
            )
            continue

        remaining = _remaining_int(check)
        # Both current media checks expose authoritative remaining-request headers.
        # Missing numeric evidence is therefore a contract drift, not permission to
        # silently fall back to a one-request assumption.
        if remaining is None:
            raise RuntimeError(
                f"{provider.upper()}_CAPACITY_HEADROOM_UNKNOWN "
                f"required_reserve={MEDIA_SEARCH_REQUEST_RESERVE}"
            )
        status = "pass" if remaining >= MEDIA_SEARCH_REQUEST_RESERVE else "insufficient_headroom"
        result = MediaCapacityMargin(
            provider=provider,
            status=status,
            remaining=remaining,
            required_reserve=MEDIA_SEARCH_REQUEST_RESERVE,
            hard_dependency=hard_dependency,
        )
        results.append(result)
        if hard_dependency and status != "pass":
            raise RuntimeError(
                f"PEXELS_CAPACITY_HEADROOM required_reserve={MEDIA_SEARCH_REQUEST_RESERVE} "
                f"remaining={remaining}"
            )

    print(
        "Observable media capacity margin: "
        + " ".join(
            f"{item.provider}={item.status}:{item.remaining}/{item.required_reserve}"
            for item in results
        )
    )
    return tuple(results)

from __future__ import annotations

import os
from pathlib import Path

import requests


TOKEN_URL = "https://oauth2.googleapis.com/token"
TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
DEFAULT_TIMEOUT_SECONDS = 20

YOUTUBE_SCOPE_PREFIX = "https://www.googleapis.com/auth/youtube"
YT_ANALYTICS_SCOPE_PREFIX = "https://www.googleapis.com/auth/yt-analytics"
REQUIRED_SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"
ALLOWED_YOUTUBE_SCOPES = frozenset(
    {
        "https://www.googleapis.com/auth/youtube.readonly",
        "https://www.googleapis.com/auth/yt-analytics.readonly",
        "https://www.googleapis.com/auth/yt-analytics-monetary.readonly",
    }
)


class YoutubeOAuthCapabilityError(RuntimeError):
    """Raised when production cannot prove that YouTube OAuth is read-only."""


def _read_nonempty(path: Path) -> str:
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise YoutubeOAuthCapabilityError(f"YouTube OAuth credential file is empty: {path.name}")
    return value


def _json_object(response: requests.Response, *, label: str) -> dict:
    try:
        payload = response.json()
    except Exception as exc:
        raise YoutubeOAuthCapabilityError(f"{label} returned malformed JSON") from exc
    if not isinstance(payload, dict):
        raise YoutubeOAuthCapabilityError(f"{label} returned non-object JSON")
    return payload


def _require_ok(response: requests.Response, *, label: str) -> None:
    if response.ok:
        return
    raise YoutubeOAuthCapabilityError(f"{label} failed: HTTP {int(response.status_code)}")


def _scope_set(raw: object) -> set[str]:
    if isinstance(raw, str):
        return {part.strip() for part in raw.split() if part.strip()}
    if isinstance(raw, list):
        return {str(part).strip() for part in raw if str(part).strip()}
    return set()


def certify_readonly_scopes(scopes: set[str]) -> None:
    """Fail closed unless every YouTube-related OAuth scope is explicitly read-only.

    Non-YouTube Google scopes are irrelevant to upload authority and are ignored here.
    Unknown future YouTube scopes are blocked by default rather than silently trusted.
    """
    youtube_related = {
        scope
        for scope in scopes
        if scope == YOUTUBE_SCOPE_PREFIX
        or scope.startswith(YOUTUBE_SCOPE_PREFIX + ".")
        or scope == YT_ANALYTICS_SCOPE_PREFIX
        or scope.startswith(YT_ANALYTICS_SCOPE_PREFIX + ".")
    }
    if not youtube_related:
        raise YoutubeOAuthCapabilityError("OAuth token exposes no certifiable YouTube/Analytics scope")
    if REQUIRED_SCOPE not in youtube_related:
        raise YoutubeOAuthCapabilityError("OAuth token is missing required yt-analytics.readonly scope")

    forbidden = sorted(youtube_related - ALLOWED_YOUTUBE_SCOPES)
    if forbidden:
        raise YoutubeOAuthCapabilityError(
            "WRITE-CAPABLE_OR_UNKNOWN_YOUTUBE_OAUTH_SCOPE_BLOCKED: " + ", ".join(forbidden)
        )


def resolve_granted_scopes(
    *,
    client_id: str,
    client_secret: str,
    refresh_token: str,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> set[str]:
    """Refresh the credential and resolve its actual granted scopes without logging tokens."""
    token_response = requests.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=timeout,
    )
    _require_ok(token_response, label="YouTube OAuth refresh")
    token_payload = _json_object(token_response, label="YouTube OAuth refresh")
    access_token = str(token_payload.get("access_token") or "").strip()
    if not access_token:
        raise YoutubeOAuthCapabilityError("YouTube OAuth refresh returned no access token")

    scopes = _scope_set(token_payload.get("scope"))
    if scopes:
        return scopes

    # Google refresh responses can omit `scope`; tokeninfo gives the effective grant.
    tokeninfo_response = requests.get(
        TOKENINFO_URL,
        params={"access_token": access_token},
        timeout=timeout,
    )
    _require_ok(tokeninfo_response, label="YouTube OAuth tokeninfo")
    tokeninfo_payload = _json_object(tokeninfo_response, label="YouTube OAuth tokeninfo")
    scopes = _scope_set(tokeninfo_payload.get("scope"))
    if not scopes:
        raise YoutubeOAuthCapabilityError("Unable to prove effective YouTube OAuth scopes")
    return scopes


def enforce_readonly_oauth(
    *,
    client_id_file: Path,
    client_secret_file: Path,
    refresh_token_file: Path,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> set[str]:
    scopes = resolve_granted_scopes(
        client_id=_read_nonempty(client_id_file),
        client_secret=_read_nonempty(client_secret_file),
        refresh_token=_read_nonempty(refresh_token_file),
        timeout=timeout,
    )
    certify_readonly_scopes(scopes)
    return scopes


def enforce_from_runner_temp(*, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> set[str] | None:
    """Guard canonical GitHub Actions production before the Engine gets OAuth files.

    No OAuth files means there is no OAuth upload capability to police. Partial materialization
    is treated as corruption and blocks. If all three exist, the actual Google grant must be
    provably read-only before provider preflight/production can continue.
    """
    runner_temp_raw = str(os.environ.get("RUNNER_TEMP") or "").strip()
    if not runner_temp_raw:
        return None

    secret_dir = Path(runner_temp_raw) / "isco-secrets"
    paths = (
        secret_dir / "youtube-client-id",
        secret_dir / "youtube-client-secret",
        secret_dir / "youtube-refresh-token",
    )
    present = [path.exists() for path in paths]
    if not any(present):
        return None
    if not all(present):
        raise YoutubeOAuthCapabilityError("Partial YouTube OAuth materialization is forbidden")

    scopes = enforce_readonly_oauth(
        client_id_file=paths[0],
        client_secret_file=paths[1],
        refresh_token_file=paths[2],
        timeout=timeout,
    )
    print("YouTube OAuth capability firewall PASS: effective grant is read-only")
    return scopes

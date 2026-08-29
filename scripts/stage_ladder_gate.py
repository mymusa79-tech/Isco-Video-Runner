from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Callable

TAG_PREFIX = "stage-ladder-green-"
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def certification_tag(sha: str) -> str:
    value = str(sha or "").strip().lower()
    if not _SHA_RE.fullmatch(value):
        raise RuntimeError("Production Stage Ladder gate requires an exact 40-hex Runner SHA")
    return TAG_PREFIX + value


def require_exact_sha_stage_ladder(
    *,
    repository: str,
    sha: str,
    token: str,
    opener: Callable[..., object] = urllib.request.urlopen,
) -> str:
    repo = str(repository or "").strip()
    if repo.count("/") != 1:
        raise RuntimeError("Production Stage Ladder gate requires GITHUB_REPOSITORY")
    value = str(sha or "").strip().lower()
    tag = certification_tag(value)
    secret = str(token or "").strip()
    if not secret:
        raise RuntimeError("Production Stage Ladder gate requires GITHUB_TOKEN")
    url = f"https://api.github.com/repos/{repo}/git/ref/tags/{tag}"
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {secret}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "isco-production-stage-ladder-gate",
        },
    )
    try:
        with opener(request, timeout=20) as response:  # type: ignore[attr-defined]
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise RuntimeError(
                f"Production blocked: no Green Stage Ladder certification for Runner SHA {value}"
            ) from exc
        raise RuntimeError(f"Production Stage Ladder certification lookup failed: HTTP {exc.code}") from exc
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("Production Stage Ladder certification lookup failed closed") from exc

    if not isinstance(payload, dict) or payload.get("ref") != f"refs/tags/{tag}":
        raise RuntimeError("Production Stage Ladder certification ref is malformed")
    obj = payload.get("object")
    if not isinstance(obj, dict) or str(obj.get("type") or "") != "commit":
        raise RuntimeError("Production Stage Ladder certification does not target a commit")
    target = str(obj.get("sha") or "").strip().lower()
    if target != value:
        raise RuntimeError(
            f"Production Stage Ladder certification target mismatch: expected {value}, got {target or 'missing'}"
        )
    return tag

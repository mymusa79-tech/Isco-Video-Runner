"""Editor-pinned stock: the editor names the exact Pexels / Pixabay video or photo page.

The production package may carry ``stock.url`` for a scene. The client then fetches exactly
that asset by its id (no search, no ranking, no extra key, no cost). Nothing here decides
whether the pick is good: the existing per-clip picture review still runs afterwards as the last
line of defence, and the approved image remains the fallback for every failure.
"""
from __future__ import annotations

import re
import urllib.parse
from typing import Any, Mapping

_LOCALE = r"(?:[a-z]{2}(?:-[a-z]{2})?/)?"
_PEXELS_PATH = re.compile(rf"^/{_LOCALE}(video|photo)/(?:[^/]*-)?(\d{{3,}})/?$", re.I)
_PIXABAY_PATH = re.compile(rf"^/{_LOCALE}(videos|photos)/(?:[^/]*-)?(\d{{3,}})/?$", re.I)
_COVERR_PATH = re.compile(rf"^/{_LOCALE}videos/[a-z0-9-]+-([a-z0-9]{{8,}})/?$", re.I)
_COVERR_HOSTS = {"coverr.co", "www.coverr.co"}
_PEXELS_HOSTS = {"pexels.com", "www.pexels.com"}
_PIXABAY_HOSTS = {"pixabay.com", "www.pixabay.com"}


class PinnedStockError(RuntimeError):
    """Raised with a short machine-readable reason for the report."""


def parse_stock_url(url: Any) -> tuple[str, str, str]:
    """(provider, kind, asset_id) for a Pexels/Pixabay/Coverr video or photo page; raises ValueError otherwise."""
    if not isinstance(url, str) or not url.strip() or len(url) > 300:
        raise ValueError("stock.url must be a page link")
    parsed = urllib.parse.urlparse(url.strip())
    host = str(parsed.hostname or "").lower()
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port:
        raise ValueError("stock.url must be a plain https link")
    if host in _PEXELS_HOSTS and (match := _PEXELS_PATH.match(parsed.path)):
        return "pexels", match.group(1).lower(), match.group(2)
    if host in _PIXABAY_HOSTS and (match := _PIXABAY_PATH.match(parsed.path)):
        return "pixabay", "video" if match.group(1).lower() == "videos" else "photo", match.group(2)
    if host in _COVERR_HOSTS and (match := _COVERR_PATH.match(parsed.path)):
        return "coverr", "video", match.group(1)
    raise ValueError("stock.url must be a Pexels, Pixabay or Coverr video or photo page")


def fetch_candidate(url: str, *, portrait: bool) -> dict[str, Any]:
    """Look the asset up by id with the key the client already has. Raises PinnedStockError."""
    from .media import _get_json, _pexels_file, _read_secret

    try:
        provider, kind, asset_id = parse_stock_url(url)
    except ValueError as exc:
        raise PinnedStockError("pinned_url_invalid") from exc
    try:
        if provider == "pexels":
            key = _read_secret("PEXELS_API_KEY")
            if not key:
                raise PinnedStockError("pinned_no_api_key")
            headers = {"Authorization": key}
            if kind == "video":
                body = _get_json(f"https://api.pexels.com/videos/videos/{asset_id}", headers=headers)
                chosen = _pexels_file(body, portrait=portrait)
                if chosen is None:
                    raise PinnedStockError("pinned_no_playable_file")
                user = body.get("user") or {}
                return {"provider": "pexels", "asset_id": asset_id, "media_kind": "video",
                        "download_url": str(chosen.get("link")), "source_url": str(body.get("url") or url),
                        "creator": str(user.get("name") or ""), "creator_url": str(user.get("url") or ""),
                        "duration": float(body.get("duration") or 0.0)}
            body = _get_json(f"https://api.pexels.com/v1/photos/{asset_id}", headers=headers)
            src = body.get("src") or {}
            link = src.get("large2x") or src.get("large") or src.get("original")
            if not link:
                raise PinnedStockError("pinned_no_playable_file")
            return {"provider": "pexels", "asset_id": asset_id, "media_kind": "photo",
                    "download_url": str(link), "source_url": str(body.get("url") or url),
                    "creator": str(body.get("photographer") or ""),
                    "creator_url": str(body.get("photographer_url") or ""), "duration": 0.0}
        if provider == "coverr":
            key = _read_secret("COVERR_API_KEY")
            if not key:
                raise PinnedStockError("pinned_no_api_key")
            body = _get_json(f"https://api.coverr.co/videos/{asset_id}?urls=true",
                             headers={"Authorization": f"Bearer {key}"})
            if bool(body.get("is_ai") or body.get("ai_generated")):
                raise PinnedStockError("pinned_ai_generated")
            urls = body.get("urls") or {}
            link = str(urls.get("mp4_download") or urls.get("mp4") or "").strip() if isinstance(urls, Mapping) else ""
            if not link:
                raise PinnedStockError("pinned_no_playable_file")
            return {"provider": "coverr", "asset_id": asset_id, "media_kind": "video", "download_url": link,
                    "source_url": str(body.get("url") or url), "creator": "", "creator_url": "",
                    "duration": float(body.get("duration") or 0.0)}
        key = _read_secret("PIXABAY_API_KEY")
        if not key:
            raise PinnedStockError("pinned_no_api_key")
        if kind == "video":
            body = _get_json("https://pixabay.com/api/videos/?" + urllib.parse.urlencode({"key": key, "id": asset_id}))
        else:
            body = _get_json("https://pixabay.com/api/?" + urllib.parse.urlencode({"key": key, "id": asset_id}))
        hits = [h for h in (body.get("hits") or []) if isinstance(h, Mapping)]
        if not hits:
            raise PinnedStockError("pinned_asset_not_found")
        hit = hits[0]
        if kind == "video":
            variants = hit.get("videos") or {}
            chosen = next((variants[n] for n in ("medium", "large", "small", "tiny")
                           if isinstance(variants.get(n), Mapping) and variants[n].get("url")), None)
            if chosen is None:
                raise PinnedStockError("pinned_no_playable_file")
            link, duration = chosen["url"], float(hit.get("duration") or 0.0)
        else:
            link, duration = hit.get("largeImageURL") or hit.get("webformatURL"), 0.0
            if not link:
                raise PinnedStockError("pinned_no_playable_file")
        return {"provider": "pixabay", "asset_id": asset_id, "media_kind": kind, "download_url": str(link),
                "source_url": str(hit.get("pageURL") or url), "creator": str(hit.get("user") or ""),
                "creator_url": "", "duration": duration}
    except PinnedStockError:
        raise
    except Exception as exc:  # network, HTTP 404, malformed JSON: the approved image covers it
        raise PinnedStockError(f"pinned_fetch_failed:{type(exc).__name__}") from exc

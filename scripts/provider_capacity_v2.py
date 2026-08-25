from __future__ import annotations

import json
import os
import tempfile
import time
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from isco_video_agent.providers import pixabay as pixabay_provider
from isco_video_agent.stock_search_cache import stock_search_cache as process_cache


PIXABAY_CACHE_TTL_SECONDS = 24 * 60 * 60
PIXABAY_CACHE_MAX_ENTRIES = 64
CACHE_SCHEMA_VERSION = 2
_INSTALLED = False


def _normalize(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def _cache_path() -> Path:
    explicit = (os.environ.get("ISCO_PIXABAY_CACHE_PATH") or "").strip()
    if explicit:
        return Path(explicit)
    root = Path(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir())
    return root / "isco-pixabay-api-cache" / "search-cache-v2.json"


def _canonical_key(*, media_kind: str, query: str, orientation: str, per_page: int) -> str:
    return "\x1f".join(
        (
            "pixabay",
            _normalize(media_kind),
            _normalize(query),
            _normalize(orientation),
            str(max(0, int(per_page))),
        )
    )


def _empty_document() -> dict:
    return {"schema_version": CACHE_SCHEMA_VERSION, "entries": {}}


def _load(path: Path) -> dict:
    if not path.is_file():
        return _empty_document()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return _empty_document()
    if not isinstance(data, dict) or data.get("schema_version") != CACHE_SCHEMA_VERSION:
        return _empty_document()
    entries = data.get("entries")
    if not isinstance(entries, dict):
        return _empty_document()
    return {"schema_version": CACHE_SCHEMA_VERSION, "entries": entries}


def _atomic_write(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(document, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _fresh_entry(entry: object, *, now: float) -> list[dict] | None:
    if not isinstance(entry, dict):
        return None
    try:
        fetched_at = float(entry.get("fetched_at"))
    except (TypeError, ValueError):
        return None
    # Future timestamps are not trusted: a corrupted/restored clock must not create
    # an effectively permanent cache entry.
    age = now - fetched_at
    if age < -300 or age < 0 or age >= PIXABAY_CACHE_TTL_SECONDS:
        return None
    response = entry.get("response")
    if not isinstance(response, list) or not all(isinstance(item, dict) for item in response):
        return None
    return deepcopy(response)


def _prune(entries: dict, *, now: float) -> dict:
    survivors: list[tuple[float, str, dict]] = []
    for key, entry in entries.items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            continue
        if _fresh_entry(entry, now=now) is None:
            continue
        try:
            fetched_at = float(entry.get("fetched_at"))
        except (TypeError, ValueError):
            continue
        survivors.append((fetched_at, key, entry))
    survivors.sort(reverse=True)
    return {key: entry for _, key, entry in survivors[:PIXABAY_CACHE_MAX_ENTRIES]}


@dataclass
class PersistentPixabaySearchCache:
    """24-hour normalized-result cache for Pixabay API searches only.

    It intentionally stores no API key, raw HTTP body, headers, or security decision.
    Media bytes are still freshly downloaded and inspected by Media Trust Boundary V2.
    Pexels and every AI/provider budget remain untouched.
    """

    path: Path
    hits: int = 0
    misses: int = 0

    def get_or_fetch(
        self,
        *,
        provider: str,
        media_kind: str,
        query: str,
        orientation: str,
        per_page: int,
        fetch: Callable[[], list[dict]],
    ) -> list[dict]:
        if _normalize(provider) != "pixabay":
            return process_cache.get_or_fetch(
                provider=provider,
                media_kind=media_kind,
                query=query,
                orientation=orientation,
                per_page=per_page,
                fetch=fetch,
            )

        key = _canonical_key(
            media_kind=media_kind,
            query=query,
            orientation=orientation,
            per_page=per_page,
        )
        now = time.time()
        document = _load(self.path)
        entries = document["entries"]
        cached = _fresh_entry(entries.get(key), now=now)
        if cached is not None:
            self.hits += 1
            return cached

        # Never cache exceptions or malformed provider responses.
        result = fetch()
        if not isinstance(result, list) or not all(isinstance(item, dict) for item in result):
            raise RuntimeError("Pixabay search provider returned invalid normalized results")
        frozen = deepcopy(result)
        entries[key] = {"fetched_at": now, "response": frozen}
        document["entries"] = _prune(entries, now=now)
        _atomic_write(self.path, document)
        self.misses += 1
        return deepcopy(frozen)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
        self.hits = 0
        self.misses = 0

    def __len__(self) -> int:
        now = time.time()
        return len(_prune(_load(self.path)["entries"], now=now))


persistent_pixabay_cache = PersistentPixabaySearchCache(_cache_path())


def install_provider_capacity_v2() -> None:
    """Install only the Pixabay 24h search cache; no retry/budget behavior changes."""
    global _INSTALLED, persistent_pixabay_cache
    if _INSTALLED:
        return
    persistent_pixabay_cache = PersistentPixabaySearchCache(_cache_path())
    pixabay_provider.stock_search_cache = persistent_pixabay_cache
    _INSTALLED = True
    print(f"Provider Capacity V2 installed: Pixabay search cache ttl=24h path={persistent_pixabay_cache.path}")
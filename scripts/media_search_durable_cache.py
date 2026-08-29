from __future__ import annotations

import hashlib
import json
import os
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

from isco_video_agent.providers import pexels as pexels_provider


CACHE_SCHEMA_VERSION = 1
CACHE_NAMESPACE = "media-search-v1"
CACHE_TTL_SECONDS = 24 * 60 * 60
MAX_ENTRIES = 64
MAX_CACHE_BYTES = 16 * 1024 * 1024
_INSTALLED = False
_ORIGINAL_SEARCH_CACHE = None


def _root() -> Path | None:
    value = (os.environ.get("ISCO_MEDIA_CACHE_PATH") or "").strip()
    return Path(value) if value else None


def _path(root: Path) -> Path:
    return root / "search" / "pexels-v1.json"


def _normalize(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def _required_env(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise RuntimeError(f"media_search_cache_missing_contract_env:{name}")
    return value


def _contract() -> dict[str, Any]:
    return {
        "namespace": CACHE_NAMESPACE,
        "schema_version": CACHE_SCHEMA_VERSION,
        "engine_sha": _required_env("ISCO_ENGINE_SHA"),
        "approved_brief_sha256": _required_env("ISCO_APPROVED_BRIEF_SHA256"),
    }


def _key(*, provider: str, media_kind: str, query: str, orientation: str, per_page: int) -> str:
    canonical = "\x1f".join(
        (
            _normalize(provider),
            _normalize(media_kind),
            _normalize(query),
            _normalize(orientation),
            str(max(0, int(per_page))),
        )
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _empty() -> dict[str, Any]:
    return {"schema_version": CACHE_SCHEMA_VERSION, "contract": _contract(), "entries": {}}


def _valid_response(value: object) -> list[dict] | None:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        return None
    return deepcopy(value)


def _fresh(entry: object, *, now: float) -> list[dict] | None:
    if not isinstance(entry, dict):
        return None
    try:
        fetched_at = float(entry.get("fetched_at"))
    except (TypeError, ValueError):
        return None
    age = now - fetched_at
    if age < 0 or age >= CACHE_TTL_SECONDS:
        return None
    return _valid_response(entry.get("response"))


def _load(path: Path) -> dict[str, Any]:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_CACHE_BYTES:
            return _empty()
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError):
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    if data.get("schema_version") != CACHE_SCHEMA_VERSION or data.get("contract") != _contract():
        return _empty()
    entries = data.get("entries")
    if not isinstance(entries, dict):
        return _empty()
    return {"schema_version": CACHE_SCHEMA_VERSION, "contract": _contract(), "entries": entries}


def _atomic_write(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(document, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        if tmp.stat().st_size > MAX_CACHE_BYTES:
            raise RuntimeError("media_search_cache_size_limit")
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _prune(entries: dict[str, Any], *, now: float) -> dict[str, Any]:
    valid: list[tuple[float, str, dict[str, Any]]] = []
    for key, entry in entries.items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            continue
        if _fresh(entry, now=now) is None:
            continue
        try:
            fetched_at = float(entry["fetched_at"])
        except (KeyError, TypeError, ValueError):
            continue
        valid.append((fetched_at, key, entry))
    valid.sort(reverse=True)
    return {key: entry for _, key, entry in valid[:MAX_ENTRIES]}


class DurablePexelsSearchCache:
    """Cross-run Pexels metadata reuse; provider keys/HTTP headers are never persisted."""

    def __init__(self, path: Path, fallback) -> None:
        self.path = Path(path)
        self.fallback = fallback
        self.hits = 0
        self.misses = 0

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
        if _normalize(provider) != "pexels":
            return self.fallback.get_or_fetch(
                provider=provider,
                media_kind=media_kind,
                query=query,
                orientation=orientation,
                per_page=per_page,
                fetch=fetch,
            )

        key = _key(
            provider=provider,
            media_kind=media_kind,
            query=query,
            orientation=orientation,
            per_page=per_page,
        )
        now = time.time()
        document = _load(self.path)
        entries = document["entries"]
        cached = _fresh(entries.get(key), now=now)
        if cached is not None:
            self.hits += 1
            print(f"Media durable Pexels search HIT key={key[:12]}")
            return cached

        result = self.fallback.get_or_fetch(
            provider=provider,
            media_kind=media_kind,
            query=query,
            orientation=orientation,
            per_page=per_page,
            fetch=fetch,
        )
        frozen = _valid_response(result)
        if frozen is None:
            raise RuntimeError("Pexels search provider returned invalid normalized results")
        entries[key] = {"fetched_at": now, "response": frozen}
        document["entries"] = _prune(entries, now=now)
        _atomic_write(self.path, document)
        self.misses += 1
        return deepcopy(frozen)


def install_media_search_durable_cache() -> None:
    global _INSTALLED, _ORIGINAL_SEARCH_CACHE
    root = _root()
    if root is None:
        print("Media durable Pexels search cache disabled: ISCO_MEDIA_CACHE_PATH not configured")
        return
    if _INSTALLED:
        return
    _ORIGINAL_SEARCH_CACHE = pexels_provider.stock_search_cache
    pexels_provider.stock_search_cache = DurablePexelsSearchCache(_path(root), _ORIGINAL_SEARCH_CACHE)
    _INSTALLED = True
    print("Media durable Pexels search cache installed: 24h normalized metadata reuse, no provider key persistence")


def reset_media_search_durable_cache_for_tests() -> None:
    global _INSTALLED, _ORIGINAL_SEARCH_CACHE
    if _ORIGINAL_SEARCH_CACHE is not None:
        pexels_provider.stock_search_cache = _ORIGINAL_SEARCH_CACHE
    _ORIGINAL_SEARCH_CACHE = None
    _INSTALLED = False


def prepare_cache_for_persistence(root: Path) -> bool:
    target = _path(Path(root))
    if target.is_symlink():
        target.unlink(missing_ok=True)
        return False
    if not target.is_file():
        return False
    document = _load(target)
    entries = _prune(document.get("entries", {}), now=time.time())
    if not entries:
        target.unlink(missing_ok=True)
        return False
    document["entries"] = entries
    try:
        _atomic_write(target, document)
    except Exception:
        target.unlink(missing_ok=True)
        return False
    return True

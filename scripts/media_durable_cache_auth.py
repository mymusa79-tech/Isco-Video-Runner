from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
from pathlib import Path
from typing import Any, Callable


HMAC_FIELD = "hmac_sha256"
HMAC_KEY_FILE_ENV = "ISCO_MEDIA_CACHE_HMAC_KEY_FILE"

_installed = False
_original_read_manifest: Callable[..., Any] | None = None
_original_store_entry: Callable[..., Any] | None = None


class MediaCacheAuthenticationError(RuntimeError):
    pass


def _key() -> bytes:
    raw_path = str(os.environ.get(HMAC_KEY_FILE_ENV) or "").strip()
    if not raw_path:
        raise MediaCacheAuthenticationError("media_cache_hmac_key_file_missing")
    path = Path(raw_path)
    if path.is_symlink() or not path.is_file():
        raise MediaCacheAuthenticationError("media_cache_hmac_key_file_invalid")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise MediaCacheAuthenticationError("media_cache_hmac_key_permissions_too_open")
    key = path.read_bytes().strip()
    if len(key) < 32:
        raise MediaCacheAuthenticationError("media_cache_hmac_key_too_short")
    return key


def _canonical_unsigned(document: dict[str, Any]) -> bytes:
    unsigned = {key: value for key, value in document.items() if key != HMAC_FIELD}
    return json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sign_document(document: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise MediaCacheAuthenticationError("media_cache_document_not_object")
    signed = dict(document)
    signed[HMAC_FIELD] = hmac.new(
        _key(), _canonical_unsigned(signed), hashlib.sha256
    ).hexdigest()
    return signed


def verify_document(document: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise MediaCacheAuthenticationError("media_cache_document_not_object")
    supplied = document.get(HMAC_FIELD)
    if not isinstance(supplied, str) or len(supplied) != 64:
        raise MediaCacheAuthenticationError("media_cache_hmac_missing_or_invalid")
    expected = hmac.new(
        _key(), _canonical_unsigned(document), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(supplied, expected):
        raise MediaCacheAuthenticationError("media_cache_hmac_mismatch")
    return document


def install_media_cache_auth() -> None:
    """Authenticate every raw/prepared manifest before it can authorize cache reuse."""
    global _installed, _original_read_manifest, _original_store_entry
    if _installed:
        return

    from scripts import media_durable_asset_cache as asset_cache

    _original_read_manifest = asset_cache._read_manifest
    _original_store_entry = asset_cache._store_entry

    def authenticated_read_manifest(*args, **kwargs):
        assert _original_read_manifest is not None
        manifest, payload = _original_read_manifest(*args, **kwargs)
        verify_document(manifest)
        return manifest, payload

    def authenticated_store_entry(*, manifest: dict[str, Any], **kwargs):
        assert _original_store_entry is not None
        return _original_store_entry(manifest=sign_document(manifest), **kwargs)

    authenticated_read_manifest._isco_media_cache_authenticated = True
    authenticated_store_entry._isco_media_cache_authenticated = True
    asset_cache._read_manifest = authenticated_read_manifest
    asset_cache._store_entry = authenticated_store_entry
    _installed = True

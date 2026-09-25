from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping

_ASSET_DIR = Path(__file__).resolve().parent / "assets" / "music"
CATALOG_PATH = _ASSET_DIR / "library.json"
CACHE_DIR = _ASSET_DIR / "cache"
MAX_TRACK_BYTES = 20 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 45

_FOCUS_TERMS = (
    "وقت", "مهمة", "مهام", "عمل", "تركيز", "تنظيم", "قائمة", "إنتاج", "خطة",
    "time", "task", "work", "focus", "productivity", "plan",
)
_HOPEFUL_TERMS = (
    "دافع", "نهوض", "انهض", "أفوز", "فوز", "نجاح", "بداية", "أمل", "تقدم",
    "motivation", "rise", "win", "success", "hope", "progress",
)


def _truthy(name: str) -> bool:
    return str(os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def load_catalog() -> dict[str, Any]:
    try:
        value = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("music_catalog_unavailable") from exc
    tracks = value.get("tracks") if isinstance(value, Mapping) else None
    if not isinstance(tracks, list) or len(tracks) < 3:
        raise RuntimeError("music_catalog_requires_three_tracks")
    return dict(value)


def _git_blob_sha1(data: bytes) -> str:
    header = f"blob {len(data)}\0".encode("ascii")
    return hashlib.sha1(header + data).hexdigest()


def _verified(path: Path, expected_sha1: str) -> bool:
    try:
        data = path.read_bytes()
    except OSError:
        return False
    return (
        1024 < len(data) <= MAX_TRACK_BYTES
        and _git_blob_sha1(data) == str(expected_sha1 or "").strip().lower()
    )


def _download(track: Mapping[str, Any], destination: Path) -> Path:
    url = str(track.get("source_url") or "").strip()
    expected = str(track.get("git_blob_sha1") or "").strip().lower()
    if not url.startswith("https://raw.githubusercontent.com/0lhi/FreePD/"):
        raise RuntimeError("music_source_not_allowlisted")
    if len(expected) != 40:
        raise RuntimeError("music_blob_identity_invalid")

    request = urllib.request.Request(url, headers={"User-Agent": "Isco-Clean-V2-CC0-Music/1"})
    try:
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            data = response.read(MAX_TRACK_BYTES + 1)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"music_download_unavailable:{type(exc).__name__}") from exc
    if len(data) > MAX_TRACK_BYTES:
        raise RuntimeError("music_download_too_large")
    if _git_blob_sha1(data) != expected:
        raise RuntimeError("music_blob_identity_mismatch")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, destination)
    return destination


def ensure_music_library(*, allow_download: bool | None = None) -> dict[str, Any]:
    catalog = load_catalog()
    allowed = _truthy("CLEAN_V2_MUSIC_ALLOW_DOWNLOAD") if allow_download is None else bool(allow_download)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    ready: list[dict[str, Any]] = []
    unavailable: list[dict[str, str]] = []

    for raw in catalog["tracks"]:
        if not isinstance(raw, Mapping):
            continue
        track = dict(raw)
        path = CACHE_DIR / str(track.get("filename") or "")
        expected = str(track.get("git_blob_sha1") or "")
        if _verified(path, expected):
            track["path"] = str(path)
            track["cache_status"] = "hit"
            ready.append(track)
            continue
        path.unlink(missing_ok=True)
        if not allowed:
            unavailable.append({"id": str(track.get("id") or ""), "reason": "download_disabled"})
            continue
        try:
            _download(track, path)
            track["path"] = str(path)
            track["cache_status"] = "downloaded"
            ready.append(track)
        except Exception as exc:
            path.unlink(missing_ok=True)
            unavailable.append({"id": str(track.get("id") or ""), "reason": str(exc)[:120]})

    return {
        "source": catalog.get("source"),
        "license": catalog.get("license"),
        "license_url": catalog.get("license_url"),
        "ready": ready,
        "unavailable": unavailable,
        "allow_download": allowed,
    }


def _script_text(script: Mapping[str, Any] | None) -> str:
    if not isinstance(script, Mapping):
        return ""
    parts = [str(script.get("title") or "")]
    for row in script.get("sections") or []:
        if isinstance(row, Mapping):
            parts.append(str(row.get("narration") or ""))
    return " ".join(parts).lower()


def select_music_track(
    script: Mapping[str, Any] | None,
    *,
    allow_download: bool | None = None,
) -> tuple[Path | None, dict[str, Any]]:
    report = ensure_music_library(allow_download=allow_download)
    ready = [item for item in report["ready"] if isinstance(item, Mapping)]
    by_id = {str(item.get("id") or ""): item for item in ready}
    text = _script_text(script)
    if any(term in text for term in _FOCUS_TERMS):
        preferred = "calm-sketch-piano"
        reason = "topic_focus_productivity"
    elif any(term in text for term in _HOPEFUL_TERMS):
        preferred = "peace-in-sunlight"
        reason = "topic_hopeful_progress"
    else:
        preferred = "a-little-faith"
        reason = "topic_general_awareness"

    chosen = by_id.get(preferred)
    if chosen is None and ready:
        chosen = sorted(ready, key=lambda item: str(item.get("id") or ""))[0]
        reason += "_fallback_available"
    report["selection_reason"] = reason
    report["selected_id"] = str(chosen.get("id") or "") if chosen else None
    report["selected_title"] = str(chosen.get("title") or "") if chosen else None
    return (Path(str(chosen["path"])), report) if chosen else (None, report)

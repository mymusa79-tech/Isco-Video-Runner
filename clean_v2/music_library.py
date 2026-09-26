from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping, Sequence

_ASSET_DIR = Path(__file__).resolve().parent / "assets" / "music"
CATALOG_PATH = _ASSET_DIR / "library.json"
CACHE_DIR = _ASSET_DIR / "cache"
MAX_TRACK_BYTES = 20 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 45
MUSIC_STUDIO_MIN_TRACKS = 6

_FORMAT_POOLS = {
    "short": {
        "focus": ("calm-sketch-piano", "acoustic-shifter", "wonder-flow"),
        "hopeful": ("peace-in-sunlight", "other-side-now", "wonder-flow"),
        "general": ("wonder-flow", "a-little-faith", "peace-in-sunlight"),
    },
    "film": {
        "focus": ("calm-sketch-piano", "c-major-2017", "all-in-silver-line"),
        "hopeful": ("other-side-now", "peace-in-sunlight", "all-in-silver-line"),
        "general": ("all-in-silver-line", "a-little-faith", "c-major-2017"),
    },
    "podcast": {
        "focus": ("wexford", "calm-sketch-piano", "a-little-faith"),
        "hopeful": ("c-major-2017", "peace-in-sunlight", "wexford"),
        "general": ("a-little-faith", "wexford", "all-in-silver-line"),
    },
}

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
    if not isinstance(tracks, list) or len(tracks) < MUSIC_STUDIO_MIN_TRACKS:
        raise RuntimeError("music_catalog_too_small_for_music_studio_lite")
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


def ensure_music_library(
    *,
    allow_download: bool | None = None,
    track_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    catalog = load_catalog()
    allowed = _truthy("CLEAN_V2_MUSIC_ALLOW_DOWNLOAD") if allow_download is None else bool(allow_download)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    ready: list[dict[str, Any]] = []
    unavailable: list[dict[str, str]] = []
    requested_ids = {
        str(item).strip()
        for item in (track_ids or ())
        if str(item).strip()
    }

    for raw in catalog["tracks"]:
        if not isinstance(raw, Mapping):
            continue
        track = dict(raw)
        track_id = str(track.get("id") or "").strip()
        if requested_ids and track_id not in requested_ids:
            continue
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
        "requested_track_ids": sorted(requested_ids),
        "catalog_track_count": len(catalog["tracks"]),
    }


def _script_text(script: Mapping[str, Any] | None) -> str:
    if not isinstance(script, Mapping):
        return ""
    parts = [str(script.get("title") or "")]
    for row in script.get("sections") or []:
        if isinstance(row, Mapping):
            parts.append(str(row.get("narration") or ""))
    return " ".join(parts).lower()


def _topic_family(text: str) -> tuple[str, str]:
    if any(term in text for term in _FOCUS_TERMS):
        return "focus", "topic_focus_productivity"
    if any(term in text for term in _HOPEFUL_TERMS):
        return "hopeful", "topic_hopeful_progress"
    return "general", "topic_general_awareness"


def _rotated_candidates(pool: tuple[str, ...], *, seed_text: str) -> tuple[list[str], int]:
    if not pool:
        return [], 0
    digest = hashlib.sha256(seed_text.encode("utf-8")).digest()
    offset = int.from_bytes(digest[:4], "big") % len(pool)
    return [*pool[offset:], *pool[:offset]], offset


def select_music_track(
    script: Mapping[str, Any] | None,
    *,
    fmt: str | None = None,
    allow_download: bool | None = None,
) -> tuple[Path | None, dict[str, Any]]:
    text = _script_text(script)
    family, reason = _topic_family(text)

    # Preserve the established deterministic single-track behavior for legacy
    # callers that do not declare a format. Production now always passes fmt.
    if fmt is None:
        preferred = {
            "focus": "calm-sketch-piano",
            "hopeful": "peace-in-sunlight",
            "general": "a-little-faith",
        }[family]
        candidates = [preferred]
        rotation_index = 0
        selection_format = "legacy"
    else:
        selection_format = str(fmt).strip().lower()
        if selection_format not in _FORMAT_POOLS:
            raise ValueError(f"music_selection_format_invalid:{fmt}")
        pool = _FORMAT_POOLS[selection_format][family]
        candidates, rotation_index = _rotated_candidates(
            pool,
            seed_text=f"{selection_format}|{family}|{text}",
        )

    # Lazy materialization: only the small candidate pool is verified/downloaded.
    # Expanding the catalog therefore does not expand first-run network work.
    report = dict(
        ensure_music_library(
            allow_download=allow_download,
            track_ids=candidates,
        )
    )
    ready = [item for item in report["ready"] if isinstance(item, Mapping)]
    by_id = {str(item.get("id") or ""): item for item in ready}
    chosen = next((by_id[item_id] for item_id in candidates if item_id in by_id), None)

    report["selection_reason"] = reason if chosen is not None else reason + "_candidate_pool_unavailable"
    report["selection_family"] = family
    report["selection_format"] = selection_format
    report["selection_candidates"] = candidates
    report["selection_rotation_index"] = rotation_index
    report["selected_id"] = str(chosen.get("id") or "") if chosen else None
    report["selected_title"] = str(chosen.get("title") or "") if chosen else None
    return (Path(str(chosen["path"])), report) if chosen else (None, report)

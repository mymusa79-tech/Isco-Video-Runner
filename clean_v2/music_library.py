from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Mapping, Sequence

_ASSET_DIR = Path(__file__).resolve().parent / "assets" / "music"
CATALOG_PATH = _ASSET_DIR / "library.json"
CACHE_DIR = _ASSET_DIR / "cache"
MAX_TRACK_BYTES = 20 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 45
MUSIC_STUDIO_MIN_TRACKS = 6
COVERR_MUSIC_PAGE_SIZE = 12
COVERR_MUSIC_CACHE_DIR = CACHE_DIR / "coverr"
COVERR_MUSIC_LICENSE_URL = "https://coverr.co/license"

_COVERR_MUSIC_TAGS = {
    "focus": "piano",
    "hopeful": "hopeful",
    "general": "peaceful",
}
_COVERR_VOCAL_MARKERS = frozenset({
    "vocal", "vocals", "backing vocal", "backing vocals", "choir",
    "singer", "singing", "lyrics", "lyric", "rap", "spoken word", "voice",
})
_COVERR_DIALOGUE_BED_MARKERS = frozenset({
    "piano", "ambient", "peaceful", "relaxing", "dreamy", "smooth",
    "elegant", "hopeful", "strings", "acoustic",
})
_COVERR_ATTENTION_MARKERS = frozenset({
    "epic", "euphoric", "heavy", "frantic", "angry", "scary",
    "suspense", "suspenseful", "running", "party", "chasing",
})

# Curated dialogue beds only. These are intentionally restrained instrumental
# cues; production must never select a vocal/lyric-forward or attention-seeking
# song under narration.
DIALOGUE_BED_TRACKS = frozenset({
    "calm-sketch-piano",
    "a-little-faith",
    "wexford",
    "c-major-2017",
})

_FORMAT_POOLS = {
    "short": {
        "focus": ("calm-sketch-piano", "a-little-faith"),
        "hopeful": ("a-little-faith", "calm-sketch-piano"),
        "general": ("calm-sketch-piano", "a-little-faith"),
    },
    "film": {
        "focus": ("calm-sketch-piano", "c-major-2017", "a-little-faith"),
        "hopeful": ("c-major-2017", "a-little-faith", "calm-sketch-piano"),
        "general": ("a-little-faith", "calm-sketch-piano", "c-major-2017"),
    },
    "podcast": {
        "focus": ("wexford", "calm-sketch-piano", "a-little-faith"),
        "hopeful": ("a-little-faith", "wexford", "calm-sketch-piano"),
        "general": ("wexford", "a-little-faith", "calm-sketch-piano"),
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


def _read_secret(name: str) -> str:
    direct = str(os.environ.get(name) or "").strip()
    if direct:
        return direct
    secret_file = str(os.environ.get(f"{name}_FILE") or "").strip()
    if not secret_file:
        return ""
    try:
        return Path(secret_file).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _safe_coverr_audio_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(str(url or ""))
    host = str(parsed.hostname or "").casefold()
    return (
        parsed.scheme == "https"
        and (host == "coverr.co" or host.endswith(".coverr.co"))
    )


def _tag_text(value: object) -> str:
    values: list[str] = []
    if isinstance(value, str):
        values.append(value)
    elif isinstance(value, Mapping):
        for key in ("name", "slug", "title", "label"):
            if value.get(key):
                values.append(str(value[key]))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            values.append(_tag_text(item))
    return " ".join(" ".join(values).split())


def _coverr_audio_metadata(item: Mapping[str, Any]) -> str:
    parts = [
        str(item.get("title") or ""),
        str(item.get("description") or ""),
        _tag_text(item.get("tags")),
        _tag_text(item.get("audio_tags")),
        _tag_text(item.get("genres")),
        _tag_text(item.get("instruments")),
        _tag_text(item.get("moods")),
    ]
    return " ".join(" ".join(parts).split()).casefold()


def _coverr_audio_download(item: Mapping[str, Any]) -> tuple[str, str] | None:
    """Return one free stock audio URL without assuming one API response shape."""
    direct_keys = (
        ("mp3_url", ".mp3"),
        ("mp3", ".mp3"),
        ("download_url", ""),
        ("audio_url", ""),
        ("file_url", ""),
        ("wav_url", ".wav"),
        ("wav", ".wav"),
    )
    for key, suffix in direct_keys:
        value = item.get(key)
        if isinstance(value, str) and _safe_coverr_audio_url(value):
            lower = urllib.parse.urlparse(value).path.casefold()
            ext = suffix or (".wav" if lower.endswith(".wav") else ".mp3")
            return value, ext

    for container_key in ("urls", "files", "sources", "downloads"):
        container = item.get(container_key)
        if isinstance(container, Mapping):
            ordered = sorted(
                container.items(),
                key=lambda pair: (
                    0 if "mp3" in str(pair[0]).casefold() else
                    1 if "download" in str(pair[0]).casefold() else
                    2 if "wav" in str(pair[0]).casefold() else 3,
                    str(pair[0]),
                ),
            )
            for key, value in ordered:
                if isinstance(value, Mapping):
                    value = value.get("url") or value.get("src") or value.get("download_url")
                if not isinstance(value, str) or not _safe_coverr_audio_url(value):
                    continue
                label = str(key).casefold()
                path = urllib.parse.urlparse(value).path.casefold()
                if "mp3" in label or path.endswith(".mp3"):
                    return value, ".mp3"
                if "wav" in label or path.endswith(".wav"):
                    return value, ".wav"
                if "download" in label or "audio" in label:
                    return value, ".mp3"
        elif isinstance(container, Sequence) and not isinstance(container, (str, bytes, bytearray)):
            ranked: list[tuple[int, str, str]] = []
            for entry in container:
                if not isinstance(entry, Mapping):
                    continue
                value = str(entry.get("url") or entry.get("src") or entry.get("download_url") or "").strip()
                if not _safe_coverr_audio_url(value):
                    continue
                hint = " ".join(str(entry.get(key) or "") for key in ("format", "type", "mime_type", "quality")).casefold()
                path = urllib.parse.urlparse(value).path.casefold()
                if "mp3" in hint or path.endswith(".mp3"):
                    ranked.append((0, value, ".mp3"))
                elif "wav" in hint or path.endswith(".wav"):
                    ranked.append((1, value, ".wav"))
            if ranked:
                _rank, value, ext = min(ranked)
                return value, ext
    return None


def _coverr_audio_hits(body: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    for key in ("hits", "audios", "items", "data", "results"):
        value = body.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, Mapping)]
        if isinstance(value, Mapping):
            nested = value.get("items") or value.get("hits") or value.get("audios")
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, Mapping)]
    return []


def _coverr_music_candidate_ok(item: Mapping[str, Any]) -> bool:
    if bool(item.get("is_ai") or item.get("ai_generated") or item.get("is_premium") or item.get("premium")):
        return False
    source_type = " ".join(
        str(item.get(key) or "")
        for key in ("source", "type", "content_type", "origin")
    ).casefold()
    if any(marker in source_type for marker in ("ai_generated", "generated", "premiumbeat", "shutterstock")):
        return False
    metadata = _coverr_audio_metadata(item)
    if not metadata:
        return False
    if any(marker in metadata for marker in _COVERR_VOCAL_MARKERS):
        return False
    if any(marker in metadata for marker in _COVERR_ATTENTION_MARKERS):
        return False
    if not any(marker in metadata for marker in _COVERR_DIALOGUE_BED_MARKERS):
        return False
    return _coverr_audio_download(item) is not None


def _coverr_music_score(item: Mapping[str, Any], *, family: str, fmt: str) -> tuple[int, str]:
    metadata = _coverr_audio_metadata(item)
    score = 0
    desired = {
        "focus": ("piano", "ambient", "relaxing", "smooth"),
        "hopeful": ("hopeful", "piano", "dreamy", "elegant"),
        "general": ("peaceful", "ambient", "piano", "relaxing"),
    }[family]
    score += sum(4 for marker in desired if marker in metadata)
    if fmt == "podcast":
        score += sum(2 for marker in ("peaceful", "ambient", "smooth") if marker in metadata)
    elif fmt == "short":
        score += sum(2 for marker in ("piano", "relaxing") if marker in metadata)
    else:
        score += sum(2 for marker in ("piano", "dreamy", "hopeful") if marker in metadata)
    title = str(item.get("title") or item.get("name") or "")
    return score, title.casefold()


def _basic_audio_file_ok(path: Path, ext: str) -> bool:
    try:
        if not path.is_file() or not (1024 < path.stat().st_size <= MAX_TRACK_BYTES):
            return False
        header = path.read_bytes()[:12]
    except OSError:
        return False
    if ext == ".wav":
        return header.startswith(b"RIFF") and b"WAVE" in header
    return header.startswith(b"ID3") or (
        len(header) >= 2 and header[0] == 0xFF and (header[1] & 0xE0) == 0xE0
    )


def _download_coverr_music(url: str, destination: Path) -> Path:
    if not _safe_coverr_audio_url(url):
        raise RuntimeError("coverr_music_source_not_allowlisted")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Isco-Clean-V2-Coverr-Music/1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            data = response.read(MAX_TRACK_BYTES + 1)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"coverr_music_download_unavailable:{type(exc).__name__}") from exc
    if not (1024 < len(data) <= MAX_TRACK_BYTES):
        raise RuntimeError("coverr_music_download_size_invalid")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, destination)
    if not _basic_audio_file_ok(destination, destination.suffix.casefold()):
        destination.unlink(missing_ok=True)
        raise RuntimeError("coverr_music_audio_invalid")
    return destination


def _try_coverr_music(
    *,
    family: str,
    fmt: str,
    allow_download: bool,
) -> tuple[Path | None, dict[str, Any]]:
    report: dict[str, Any] = {
        "provider": "coverr",
        "status": "not_attempted",
        "provider_calls_added": 0,
        "tag": _COVERR_MUSIC_TAGS[family],
        "instrumental_only_required": True,
        "dialogue_bed_required": True,
        "license": "Coverr free stock music",
        "license_url": COVERR_MUSIC_LICENSE_URL,
    }
    if not allow_download:
        report["status"] = "download_disabled"
        return None, report
    key = _read_secret("COVERR_API_KEY")
    if not key:
        report["status"] = "missing_api_key"
        return None, report

    tag = _COVERR_MUSIC_TAGS[family]
    params = urllib.parse.urlencode({"page_size": COVERR_MUSIC_PAGE_SIZE})
    request = urllib.request.Request(
        f"https://api.coverr.co/audio-tags/{urllib.parse.quote(tag)}/audios?{params}",
        headers={
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "User-Agent": "Isco-Clean-V2-Coverr-Music/1",
        },
    )
    report["provider_calls_added"] = 1
    try:
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            raw = response.read(2 * 1024 * 1024)
        body = json.loads(raw.decode("utf-8"))
        if not isinstance(body, Mapping):
            raise RuntimeError("coverr_music_response_not_object")
    except Exception as exc:
        report["status"] = "search_failed"
        report["reason"] = f"{type(exc).__name__}:{str(exc)[:100]}"
        return None, report

    candidates = [item for item in _coverr_audio_hits(body) if _coverr_music_candidate_ok(item)]
    candidates.sort(key=lambda item: _coverr_music_score(item, family=family, fmt=fmt), reverse=True)
    report["safe_candidate_count"] = len(candidates)
    if not candidates:
        report["status"] = "no_safe_instrumental_candidate"
        return None, report

    chosen = candidates[0]
    download = _coverr_audio_download(chosen)
    if download is None:
        report["status"] = "no_download_url"
        return None, report
    url, ext = download
    asset_id = str(chosen.get("id") or chosen.get("slug") or "").strip()
    if not asset_id:
        asset_id = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    safe_id = re.sub(r"[^A-Za-z0-9_-]+", "-", asset_id).strip("-")[:64] or "audio"
    destination = COVERR_MUSIC_CACHE_DIR / f"{safe_id}{ext}"
    try:
        if not _basic_audio_file_ok(destination, ext):
            destination.unlink(missing_ok=True)
            _download_coverr_music(url, destination)
        report.update({
            "status": "selected",
            "selected_id": asset_id,
            "selected_title": str(chosen.get("title") or chosen.get("name") or asset_id),
            "selected_metadata": _coverr_audio_metadata(chosen)[:500],
            "selected_instrumental_only": True,
            "selected_dialogue_bed": True,
            "selected_origin": "coverr_free_stock_music",
            "selected_sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
        })
        return destination, report
    except Exception as exc:
        destination.unlink(missing_ok=True)
        report["status"] = "download_failed"
        report["reason"] = f"{type(exc).__name__}:{str(exc)[:100]}"
        return None, report


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
    allowed = _truthy("CLEAN_V2_MUSIC_ALLOW_DOWNLOAD") if allow_download is None else bool(allow_download)

    # Preserve the established deterministic single-track behavior for legacy
    # callers that do not declare a format. Production always passes fmt.
    if fmt is None:
        preferred = {
            "focus": "calm-sketch-piano",
            "hopeful": "peace-in-sunlight",
            "general": "a-little-faith",
        }[family]
        candidates = [preferred]
        rotation_index = 0
        selection_format = "legacy"
        coverr_path = None
        coverr_report: dict[str, Any] = {
            "provider": "coverr",
            "status": "legacy_not_attempted",
            "provider_calls_added": 0,
        }
    else:
        selection_format = str(fmt).strip().lower()
        if selection_format not in _FORMAT_POOLS:
            raise ValueError(f"music_selection_format_invalid:{fmt}")
        pool = _FORMAT_POOLS[selection_format][family]
        candidates, rotation_index = _rotated_candidates(
            pool,
            seed_text=f"{selection_format}|{family}|{text}",
        )
        # One bounded Coverr stock-music lookup per production. It is optional:
        # any API/schema/network/license-safety miss falls straight back to the
        # existing verified FreePD dialogue beds.
        coverr_path, coverr_report = _try_coverr_music(
            family=family,
            fmt=selection_format,
            allow_download=allowed,
        )
        if coverr_path is not None:
            report = {
                "source": "coverr",
                "license": coverr_report.get("license"),
                "license_url": coverr_report.get("license_url"),
                "allow_download": allowed,
                "ready": [],
                "unavailable": [],
                "catalog_track_count": 0,
                "instrumental_only_required": True,
                "dialogue_bed_required": True,
                "dialogue_bed_allowlist": sorted(DIALOGUE_BED_TRACKS),
                "selection_reason": reason + "_coverr_free_stock_music",
                "selection_family": family,
                "selection_format": selection_format,
                "selection_candidates": [coverr_report.get("selected_id")],
                "selection_rotation_index": 0,
                "selected_id": coverr_report.get("selected_id"),
                "selected_title": coverr_report.get("selected_title"),
                "selected_instrumental_only": True,
                "selected_dialogue_bed": True,
                "selected_origin": "coverr_free_stock_music",
                "provider_calls_added": int(coverr_report.get("provider_calls_added") or 0),
                "coverr": coverr_report,
            }
            return coverr_path, report

    # Existing verified CC0 beds remain the fail-safe. This path adds no provider
    # calls and is used whenever Coverr is missing, unsafe, unavailable or disabled.
    report = dict(
        ensure_music_library(
            allow_download=allowed,
            track_ids=candidates,
        )
    )
    ready = [item for item in report["ready"] if isinstance(item, Mapping)]
    by_id = {str(item.get("id") or ""): item for item in ready}
    chosen = next(
        (
            by_id[item_id]
            for item_id in candidates
            if item_id in by_id and item_id in DIALOGUE_BED_TRACKS
        ),
        None,
    )

    report["instrumental_only_required"] = True
    report["dialogue_bed_required"] = True
    report["dialogue_bed_allowlist"] = sorted(DIALOGUE_BED_TRACKS)
    report["selection_reason"] = (
        reason if chosen is not None else reason + "_dialogue_bed_unavailable"
    )
    report["selection_family"] = family
    report["selection_format"] = selection_format
    report["selection_candidates"] = candidates
    report["selection_rotation_index"] = rotation_index
    report["selected_id"] = str(chosen.get("id") or "") if chosen else None
    report["selected_title"] = str(chosen.get("title") or "") if chosen else None
    report["selected_instrumental_only"] = bool(
        chosen and str(chosen.get("id") or "") in DIALOGUE_BED_TRACKS
    )
    report["selected_dialogue_bed"] = report["selected_instrumental_only"]
    report["selected_origin"] = "verified_cc0_freepd_instrumental_dialogue_bed" if chosen else None
    report["provider_calls_added"] = int(coverr_report.get("provider_calls_added") or 0)
    report["coverr"] = coverr_report
    return (Path(str(chosen["path"])), report) if chosen else (None, report)

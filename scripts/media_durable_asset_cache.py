from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

import isco_video_agent.media.ffmpeg as engine_ffmpeg
import isco_video_agent.orchestrator as orchestrator

from scripts import media_trust_boundary_v2 as media_trust


SCHEMA_VERSION = 1
CACHE_NAMESPACE = "media-asset-v1"
AUDIT_FILENAME = "media-durable-cache-audit.json"
RAW_FILENAME = "source.bin"
PREPARED_FILENAME = "prepared.mp4"
MANIFEST_FILENAME = "manifest.json"
MAX_CACHE_ENTRIES = 64
MAX_CACHE_BYTES = 1024 * 1024 * 1024

_original_trusted_download: Callable[..., Any] | None = None
_original_prepare_clip: Callable[..., Any] | None = None
_ffmpeg_identity_cache: str | None = None


class CacheEntryInvalid(RuntimeError):
    pass


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _module_sha256(module: Any) -> str:
    path = Path(module.__file__).resolve()
    return _sha256_file(path)


def _media_trust_contract_sha256() -> str:
    return _module_sha256(media_trust)


def _render_contract_sha256() -> str:
    # prepare_clip depends on ffmpeg.py and its color-grade module. Binding both module
    # files is conservative and deliberately rebuilds prepared clips after renderer
    # semantics change, while unrelated Runner changes leave media reuse intact.
    color_module = __import__("isco_video_agent.media.color", fromlist=["*"])
    payload = {
        "ffmpeg_module_sha256": _module_sha256(engine_ffmpeg),
        "color_module_sha256": _module_sha256(color_module),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _sha256_bytes(canonical)


def _ffmpeg_identity() -> str:
    global _ffmpeg_identity_cache
    if _ffmpeg_identity_cache is not None:
        return _ffmpeg_identity_cache
    try:
        completed = subprocess.run(
            ["ffmpeg", "-version"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
        first = (completed.stdout or "").splitlines()[0].strip()
    except Exception as exc:
        first = f"unavailable:{type(exc).__name__}"
    _ffmpeg_identity_cache = first
    return first


def _configured_cache_root() -> Path | None:
    raw = str(os.environ.get("ISCO_MEDIA_CACHE_DIR") or "").strip()
    if not raw:
        return None
    root = Path(raw)
    if root.exists() and root.is_symlink():
        raise CacheEntryInvalid("media_cache_root_symlink_rejected")
    root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir():
        raise CacheEntryInvalid("media_cache_root_not_directory")
    return root.resolve()


def _kind_root(root: Path, kind: str) -> Path:
    if kind not in {"raw", "prepared"}:
        raise CacheEntryInvalid("media_cache_kind_invalid")
    path = root / kind
    if path.exists() and path.is_symlink():
        raise CacheEntryInvalid("media_cache_kind_symlink_rejected")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _entry_dir(root: Path, kind: str, fingerprint: str) -> Path:
    if len(fingerprint) != 64 or any(ch not in "0123456789abcdef" for ch in fingerprint):
        raise CacheEntryInvalid("media_cache_fingerprint_invalid")
    parent = _kind_root(root, kind)
    entry = parent / fingerprint
    try:
        if entry.parent.resolve() != parent.resolve():
            raise CacheEntryInvalid("media_cache_path_escape")
    except OSError as exc:
        raise CacheEntryInvalid("media_cache_path_resolution_failed") from exc
    return entry


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    except Exception:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=destination.name + ".", suffix=".tmp", dir=destination.parent)
    os.close(fd)
    tmp = Path(name)
    try:
        shutil.copyfile(source, tmp)
        with tmp.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(tmp, destination)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _is_final_clip_path(path: Path) -> bool:
    # Review candidates are intentionally not persisted. Only assets that reached the
    # renderer's final clips/ directory enter the durable cache, preventing rejected
    # Vision candidates from consuming the bounded cross-run cache.
    return path.parent.name == "clips"


def _output_root_for(path: Path) -> Path | None:
    path = Path(path)
    if path.parent.name in {"clips", "visual-review"}:
        return path.parent.parent
    return None


def _append_audit(path: Path, event: dict[str, Any]) -> None:
    output_root = _output_root_for(path)
    if output_root is None:
        return
    try:
        audit_path = output_root / AUDIT_FILENAME
        if audit_path.is_file():
            document = json.loads(audit_path.read_text(encoding="utf-8"))
        else:
            document = {"schema_version": 1, "mode": "trusted_asset_and_render_resume", "events": []}
        if not isinstance(document, dict) or not isinstance(document.get("events"), list):
            document = {"schema_version": 1, "mode": "trusted_asset_and_render_resume", "events": []}
        document["events"].append(event)
        outcomes = [str(item.get("outcome") or "") for item in document["events"] if isinstance(item, dict)]
        document["summary"] = {
            "raw_hits": outcomes.count("raw_hit"),
            "raw_stored": outcomes.count("raw_stored"),
            "raw_invalidated": outcomes.count("raw_invalidated"),
            "prepared_hits": outcomes.count("prepared_hit"),
            "prepared_stored": outcomes.count("prepared_stored"),
            "prepared_invalidated": outcomes.count("prepared_invalidated"),
        }
        _atomic_json(audit_path, document)
    except Exception as exc:
        print(f"Media durable cache audit skipped ({type(exc).__name__})")


def raw_fingerprint(*, provider: str, source_url: str) -> tuple[str, dict[str, Any]]:
    provider = str(provider).strip().casefold()
    source_url = str(source_url).strip()
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "namespace": CACHE_NAMESPACE,
        "kind": "raw",
        "provider": provider,
        "source_url": source_url,
        "media_trust_contract_sha256": _media_trust_contract_sha256(),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _sha256_bytes(canonical), payload


def prepared_fingerprint(
    *,
    source_sha256: str,
    seconds: float,
    portrait: bool,
    fps: int,
) -> tuple[str, dict[str, Any]]:
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "namespace": CACHE_NAMESPACE,
        "kind": "prepared",
        "source_sha256": str(source_sha256),
        "seconds": f"{float(seconds):.6f}",
        "portrait": bool(portrait),
        "fps": int(fps),
        "render_contract_sha256": _render_contract_sha256(),
        "ffmpeg_identity": _ffmpeg_identity(),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _sha256_bytes(canonical), payload


def _evict_entry(entry: Path) -> None:
    try:
        if entry.is_symlink():
            entry.unlink(missing_ok=True)
        elif entry.exists():
            shutil.rmtree(entry)
    except OSError:
        pass


def _read_manifest(entry: Path, *, fingerprint: str, kind: str) -> tuple[dict[str, Any], Path]:
    if entry.is_symlink():
        raise CacheEntryInvalid("media_cache_entry_symlink_rejected")
    manifest_path = entry / MANIFEST_FILENAME
    payload_name = RAW_FILENAME if kind == "raw" else PREPARED_FILENAME
    payload_path = entry / payload_name
    if manifest_path.is_symlink() or payload_path.is_symlink():
        raise CacheEntryInvalid("media_cache_file_symlink_rejected")
    if not manifest_path.is_file() or not payload_path.is_file():
        raise CacheEntryInvalid("media_cache_entry_incomplete")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise CacheEntryInvalid("media_cache_manifest_invalid_json") from exc
    if not isinstance(manifest, dict):
        raise CacheEntryInvalid("media_cache_manifest_not_object")
    if manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("namespace") != CACHE_NAMESPACE:
        raise CacheEntryInvalid("media_cache_manifest_version_mismatch")
    if manifest.get("kind") != kind or manifest.get("fingerprint") != fingerprint:
        raise CacheEntryInvalid("media_cache_manifest_identity_mismatch")
    expected_size = manifest.get("payload_bytes")
    expected_sha = manifest.get("payload_sha256")
    if not isinstance(expected_size, int) or expected_size <= 0:
        raise CacheEntryInvalid("media_cache_manifest_size_invalid")
    if not isinstance(expected_sha, str) or len(expected_sha) != 64:
        raise CacheEntryInvalid("media_cache_manifest_sha_invalid")
    if payload_path.stat().st_size != expected_size:
        raise CacheEntryInvalid("media_cache_payload_size_mismatch")
    if _sha256_file(payload_path) != expected_sha:
        raise CacheEntryInvalid("media_cache_payload_sha_mismatch")
    return manifest, payload_path


def _store_entry(
    *,
    root: Path,
    kind: str,
    fingerprint: str,
    manifest: dict[str, Any],
    source: Path,
) -> None:
    if source.is_symlink() or not source.is_file() or source.stat().st_size <= 0:
        raise CacheEntryInvalid("media_cache_source_invalid")
    entry = _entry_dir(root, kind, fingerprint)
    parent = entry.parent
    tmp = Path(tempfile.mkdtemp(prefix=f".{fingerprint[:12]}-", dir=parent))
    payload_name = RAW_FILENAME if kind == "raw" else PREPARED_FILENAME
    try:
        shutil.copyfile(source, tmp / payload_name)
        _atomic_json(tmp / MANIFEST_FILENAME, manifest)
        _evict_entry(entry)
        os.replace(tmp, entry)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    _prune_cache(root)


def _touch(entry: Path) -> None:
    try:
        os.utime(entry, None)
    except OSError:
        pass


def _entry_size(entry: Path) -> int:
    total = 0
    try:
        for path in entry.iterdir():
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
    except OSError:
        return 0
    return total


def _prune_cache(root: Path) -> None:
    entries: list[tuple[float, Path, int]] = []
    for kind in ("raw", "prepared"):
        parent = root / kind
        if not parent.exists():
            continue
        if parent.is_symlink():
            parent.unlink(missing_ok=True)
            continue
        try:
            children = list(parent.iterdir())
        except OSError:
            continue
        for entry in children:
            if entry.is_symlink():
                entry.unlink(missing_ok=True)
                continue
            if not entry.is_dir():
                entry.unlink(missing_ok=True)
                continue
            try:
                stamp = entry.stat().st_mtime
            except OSError:
                stamp = 0.0
            entries.append((stamp, entry, _entry_size(entry)))
    entries.sort(key=lambda item: item[0], reverse=True)
    kept = 0
    used = 0
    for _stamp, entry, size in entries:
        if kept < MAX_CACHE_ENTRIES and used + size <= MAX_CACHE_BYTES:
            kept += 1
            used += size
            continue
        _evict_entry(entry)


def _restore_raw(
    *,
    root: Path,
    provider: str,
    source_url: str,
    fingerprint: str,
    destination: Path,
) -> dict[str, Any] | None:
    entry = _entry_dir(root, "raw", fingerprint)
    if not entry.exists():
        return None
    manifest, payload = _read_manifest(entry, fingerprint=fingerprint, kind="raw")
    semantic = manifest.get("semantic_contract")
    expected_fingerprint, expected_semantic = raw_fingerprint(provider=provider, source_url=source_url)
    if expected_fingerprint != fingerprint or semantic != expected_semantic:
        raise CacheEntryInvalid("media_cache_raw_semantic_mismatch")

    final_url = str(manifest.get("final_url") or "").strip()
    media_trust._validate_provider_url(provider, source_url)
    media_trust._validate_provider_url(provider, final_url)

    quarantine = media_trust._root() / f"durable-{fingerprint}{Path(destination).suffix or '.bin'}"
    _atomic_copy(payload, quarantine)
    record = media_trust.TrustedMediaRecord(
        provider=provider,
        source_url=source_url,
        final_url=final_url,
        sha256=str(manifest["payload_sha256"]),
        byte_length=int(manifest["payload_bytes"]),
        quarantine_path=quarantine,
    )
    media_trust._records_by_url[(provider, source_url)] = record
    result = media_trust._materialize_verified(record, Path(destination))
    _touch(entry)
    return manifest


def _store_raw(
    *,
    root: Path,
    provider: str,
    source_url: str,
    fingerprint: str,
    semantic: dict[str, Any],
    destination: Path,
) -> dict[str, Any]:
    record = media_trust.trusted_record(destination)
    if record is None:
        raise CacheEntryInvalid("media_cache_raw_missing_trust_record")
    if record.provider != provider or record.source_url != source_url:
        raise CacheEntryInvalid("media_cache_raw_trust_identity_mismatch")
    if destination.is_symlink() or not destination.is_file():
        raise CacheEntryInvalid("media_cache_raw_destination_invalid")
    if destination.stat().st_size != record.byte_length or _sha256_file(destination) != record.sha256:
        raise CacheEntryInvalid("media_cache_raw_destination_mismatch")
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "namespace": CACHE_NAMESPACE,
        "kind": "raw",
        "fingerprint": fingerprint,
        "semantic_contract": semantic,
        "provider": provider,
        "source_url": source_url,
        "final_url": record.final_url,
        "payload_sha256": record.sha256,
        "payload_bytes": record.byte_length,
        "created_unix": int(time.time()),
    }
    _store_entry(
        root=root,
        kind="raw",
        fingerprint=fingerprint,
        manifest=manifest,
        source=destination,
    )
    return manifest


def _validate_prepared_clip(path: Path, *, seconds: float, portrait: bool, fps: int) -> None:
    if path.is_symlink() or not path.is_file() or path.stat().st_size <= 0:
        raise CacheEntryInvalid("media_cache_prepared_file_invalid")
    probe = engine_ffmpeg.probe(path)
    streams = probe.get("streams", []) if isinstance(probe, dict) else []
    videos = [item for item in streams if isinstance(item, dict) and item.get("codec_type") == "video"]
    audios = [item for item in streams if isinstance(item, dict) and item.get("codec_type") == "audio"]
    if len(videos) != 1 or audios:
        raise CacheEntryInvalid("media_cache_prepared_stream_contract_failed")
    expected_w, expected_h = ((1080, 1920) if portrait else (1920, 1080))
    if int(videos[0].get("width") or 0) != expected_w or int(videos[0].get("height") or 0) != expected_h:
        raise CacheEntryInvalid("media_cache_prepared_dimensions_mismatch")
    actual = engine_ffmpeg.duration(path)
    if actual + 0.25 < float(seconds) or actual > float(seconds) + 0.75:
        raise CacheEntryInvalid("media_cache_prepared_duration_mismatch")
    rate = str(videos[0].get("avg_frame_rate") or videos[0].get("r_frame_rate") or "")
    if "/" in rate:
        numerator, denominator = rate.split("/", 1)
        try:
            actual_fps = float(numerator) / float(denominator)
        except (TypeError, ValueError, ZeroDivisionError):
            actual_fps = 0.0
        if actual_fps and abs(actual_fps - int(fps)) > 0.2:
            raise CacheEntryInvalid("media_cache_prepared_fps_mismatch")


def _restore_prepared(
    *,
    root: Path,
    fingerprint: str,
    semantic: dict[str, Any],
    destination: Path,
    seconds: float,
    portrait: bool,
    fps: int,
) -> dict[str, Any] | None:
    entry = _entry_dir(root, "prepared", fingerprint)
    if not entry.exists():
        return None
    manifest, payload = _read_manifest(entry, fingerprint=fingerprint, kind="prepared")
    if manifest.get("semantic_contract") != semantic:
        raise CacheEntryInvalid("media_cache_prepared_semantic_mismatch")
    _atomic_copy(payload, destination)
    try:
        _validate_prepared_clip(destination, seconds=seconds, portrait=portrait, fps=fps)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    if _sha256_file(destination) != manifest["payload_sha256"]:
        destination.unlink(missing_ok=True)
        raise CacheEntryInvalid("media_cache_prepared_restored_sha_mismatch")
    _touch(entry)
    return manifest


def _store_prepared(
    *,
    root: Path,
    fingerprint: str,
    semantic: dict[str, Any],
    destination: Path,
    seconds: float,
    portrait: bool,
    fps: int,
) -> dict[str, Any]:
    _validate_prepared_clip(destination, seconds=seconds, portrait=portrait, fps=fps)
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "namespace": CACHE_NAMESPACE,
        "kind": "prepared",
        "fingerprint": fingerprint,
        "semantic_contract": semantic,
        "payload_sha256": _sha256_file(destination),
        "payload_bytes": destination.stat().st_size,
        "created_unix": int(time.time()),
    }
    _store_entry(
        root=root,
        kind="prepared",
        fingerprint=fingerprint,
        manifest=manifest,
        source=destination,
    )
    return manifest


def prepare_cache_for_persistence(path: Path | None = None) -> bool:
    """Sanitize the bounded rebuildable media cache before Actions cache persistence."""
    try:
        if path is not None:
            candidate = Path(path)
            if candidate.exists() and candidate.is_symlink():
                return False
            root = candidate.resolve()
        else:
            root = _configured_cache_root()
        if root is None or not root.exists() or root.is_symlink():
            return False
        _prune_cache(root)
        valid = 0
        for kind in ("raw", "prepared"):
            parent = root / kind
            if not parent.exists():
                continue
            if parent.is_symlink():
                parent.unlink(missing_ok=True)
                continue
            for entry in list(parent.iterdir()):
                if entry.is_symlink() or not entry.is_dir():
                    _evict_entry(entry)
                    continue
                try:
                    _read_manifest(entry, fingerprint=entry.name, kind=kind)
                except Exception:
                    _evict_entry(entry)
                    continue
                valid += 1
        _prune_cache(root)
        print(f"Media durable cache sanitized for persistence: valid_entries={valid}")
        return valid > 0
    except Exception as exc:
        print(f"Media durable cache persistence skipped ({type(exc).__name__})")
        return False


def install_media_durable_asset_cache() -> None:
    """Resume trusted selected media bytes and deterministic prepared clips across runs."""
    global _original_trusted_download, _original_prepare_clip

    current_download = media_trust.trusted_download
    if getattr(current_download, "_isco_media_durable_raw_cache", False) is not True:
        _original_trusted_download = current_download

        def durable_trusted_download(provider: str, url: str, dest: Path) -> Path:
            assert _original_trusted_download is not None
            provider_name = str(provider).strip().casefold()
            source_url = str(url).strip()
            destination = Path(dest)
            try:
                root = _configured_cache_root()
            except Exception as exc:
                root = None
                print(f"Media durable raw cache disabled for this call ({type(exc).__name__})")
            if root is None:
                return _original_trusted_download(provider_name, source_url, destination)

            fingerprint, semantic = raw_fingerprint(provider=provider_name, source_url=source_url)
            entry = _entry_dir(root, "raw", fingerprint)
            try:
                manifest = _restore_raw(
                    root=root,
                    provider=provider_name,
                    source_url=source_url,
                    fingerprint=fingerprint,
                    destination=destination,
                )
                if manifest is not None:
                    _append_audit(
                        destination,
                        {
                            "outcome": "raw_hit",
                            "provider": provider_name,
                            "fingerprint": fingerprint,
                            "sha256": manifest["payload_sha256"],
                        },
                    )
                    return destination
            except Exception as exc:
                _evict_entry(entry)
                _append_audit(
                    destination,
                    {
                        "outcome": "raw_invalidated",
                        "provider": provider_name,
                        "fingerprint": fingerprint,
                        "reason": type(exc).__name__,
                    },
                )

            result = Path(_original_trusted_download(provider_name, source_url, destination))
            if _is_final_clip_path(destination):
                try:
                    manifest = _store_raw(
                        root=root,
                        provider=provider_name,
                        source_url=source_url,
                        fingerprint=fingerprint,
                        semantic=semantic,
                        destination=result,
                    )
                    _append_audit(
                        destination,
                        {
                            "outcome": "raw_stored",
                            "provider": provider_name,
                            "fingerprint": fingerprint,
                            "sha256": manifest["payload_sha256"],
                        },
                    )
                except Exception as exc:
                    print(f"Media durable raw store skipped ({type(exc).__name__})")
            return result

        durable_trusted_download._isco_media_durable_raw_cache = True
        durable_trusted_download._isco_media_durable_original = current_download
        media_trust.trusted_download = durable_trusted_download

    current_prepare = orchestrator.prepare_clip
    if getattr(current_prepare, "_isco_media_durable_prepared_cache", False) is not True:
        _original_prepare_clip = current_prepare

        def durable_prepare_clip(
            src: Path,
            dest: Path,
            seconds: float,
            portrait: bool,
            fps: int = 30,
        ) -> Path:
            assert _original_prepare_clip is not None
            source = Path(src)
            destination = Path(dest)
            try:
                root = _configured_cache_root()
            except Exception as exc:
                root = None
                print(f"Media durable prepared cache disabled for this call ({type(exc).__name__})")
            if root is None:
                return _original_prepare_clip(source, destination, seconds, portrait, fps=fps)

            trust_record = media_trust.trusted_record(source)
            if trust_record is None or source.is_symlink() or not source.is_file():
                return _original_prepare_clip(source, destination, seconds, portrait, fps=fps)
            source_sha = _sha256_file(source)
            if source_sha != trust_record.sha256 or source.stat().st_size != trust_record.byte_length:
                raise RuntimeError("media_cache_prepare_source_no_longer_matches_trust_record")

            fingerprint, semantic = prepared_fingerprint(
                source_sha256=source_sha,
                seconds=seconds,
                portrait=portrait,
                fps=fps,
            )
            entry = _entry_dir(root, "prepared", fingerprint)
            try:
                manifest = _restore_prepared(
                    root=root,
                    fingerprint=fingerprint,
                    semantic=semantic,
                    destination=destination,
                    seconds=seconds,
                    portrait=portrait,
                    fps=fps,
                )
                if manifest is not None:
                    _append_audit(
                        destination,
                        {
                            "outcome": "prepared_hit",
                            "fingerprint": fingerprint,
                            "source_sha256": source_sha,
                            "sha256": manifest["payload_sha256"],
                        },
                    )
                    return destination
            except Exception as exc:
                _evict_entry(entry)
                _append_audit(
                    destination,
                    {
                        "outcome": "prepared_invalidated",
                        "fingerprint": fingerprint,
                        "source_sha256": source_sha,
                        "reason": type(exc).__name__,
                    },
                )

            result = Path(_original_prepare_clip(source, destination, seconds, portrait, fps=fps))
            try:
                manifest = _store_prepared(
                    root=root,
                    fingerprint=fingerprint,
                    semantic=semantic,
                    destination=result,
                    seconds=seconds,
                    portrait=portrait,
                    fps=fps,
                )
                _append_audit(
                    destination,
                    {
                        "outcome": "prepared_stored",
                        "fingerprint": fingerprint,
                        "source_sha256": source_sha,
                        "sha256": manifest["payload_sha256"],
                    },
                )
            except Exception as exc:
                print(f"Media durable prepared store skipped ({type(exc).__name__})")
            return result

        durable_prepare_clip._isco_media_durable_prepared_cache = True
        durable_prepare_clip._isco_media_durable_original = current_prepare
        orchestrator.prepare_clip = durable_prepare_clip

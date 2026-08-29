from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

import isco_video_agent.orchestrator as orchestrator

from scripts import m8_live_binding as m8_live
from scripts import media_durable_cache as media_cache
from scripts import media_trust_boundary_v2 as trust

CACHE_SCHEMA_VERSION = 1
CACHE_NAMESPACE = "media-prepared-live-v1"
MAX_CLIP_BYTES = 512 * 1024 * 1024
MAX_SIDECAR_BYTES = 1024 * 1024
MAX_ENTRIES = 32
MAX_TOTAL_BYTES = 1024 * 1024 * 1024

_ORIGINAL_PRODUCE: Callable[..., Any] | None = None
_INSTALLED = False


def _required_env(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise RuntimeError(f"media_prepared_live_missing_contract_env:{name}")
    return value


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return media_cache._sha256_file(Path(path))


def _module_sha(module: Any) -> str:
    module_file = getattr(module, "__file__", None)
    if not module_file:
        return "unknown"
    return _sha256_file(Path(module_file).resolve())


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _binding_hash(binding: dict[str, Any]) -> str:
    return _sha256_bytes(_json_bytes(binding))


def _cache_root() -> Path | None:
    media_root = media_cache._cache_root()
    return Path(media_root) / "prepared-live" if media_root is not None else None


def _renderer_identity(renderer: Callable[..., Any]) -> dict[str, str]:
    module_name = str(getattr(renderer, "__module__", "") or "")
    module = sys.modules.get(module_name)
    return {
        "module": module_name,
        "module_sha256": _module_sha(module) if module is not None else "unknown",
        "qualname": str(getattr(renderer, "__qualname__", getattr(renderer, "__name__", "unknown"))),
    }


def _binding(record: Any, renderer: Callable[..., Any], *, seconds: float, portrait: bool, fps: int) -> dict[str, Any]:
    return {
        "namespace": CACHE_NAMESPACE,
        "schema_version": CACHE_SCHEMA_VERSION,
        "approved_brief_sha256": _required_env("ISCO_APPROVED_BRIEF_SHA256"),
        "engine_sha": _required_env("ISCO_ENGINE_SHA"),
        "bridge_sha256": _module_sha(sys.modules[__name__]),
        "m8_live_sha256": _module_sha(m8_live),
        "renderer": _renderer_identity(renderer),
        "provider": str(record.provider).strip().casefold(),
        "source_url": str(record.source_url).strip(),
        "source_sha256": str(record.sha256),
        "seconds_millis": int(round(float(seconds) * 1000.0)),
        "portrait": bool(portrait),
        "fps": int(fps),
    }


def _entry(root: Path, fingerprint: str) -> Path:
    return Path(root) / fingerprint


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _atomic_copy(source: Path, dest: Path) -> None:
    media_cache._atomic_copy(Path(source), Path(dest))


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    media_cache._atomic_json(Path(path), payload)


def _sidecar_source(dest: Path) -> Path:
    return Path(dest).with_suffix(".m8.json")


def _validate_sidecar(entry: Path, manifest: dict[str, Any]) -> Path | None:
    meta = manifest.get("m8_sidecar")
    if meta is None:
        return None
    if not isinstance(meta, dict):
        raise RuntimeError("media_prepared_live_invalid_sidecar_manifest")
    filename = str(meta.get("filename") or "")
    if not filename or Path(filename).name != filename:
        raise RuntimeError("media_prepared_live_invalid_sidecar_filename")
    path = entry / filename
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("media_prepared_live_missing_sidecar")
    size = path.stat().st_size
    if size <= 0 or size > MAX_SIDECAR_BYTES or meta.get("byte_length") != size:
        raise RuntimeError("media_prepared_live_invalid_sidecar_size")
    sha = str(meta.get("sha256") or "")
    if not sha or _sha256_file(path) != sha:
        raise RuntimeError("media_prepared_live_invalid_sidecar_sha")
    if _load_json(path) is None:
        raise RuntimeError("media_prepared_live_invalid_sidecar_json")
    return path


def _validate_entry(root: Path, fingerprint: str, expected_binding: dict[str, Any] | None = None):
    entry = _entry(root, fingerprint)
    manifest = _load_json(entry / "manifest.json")
    clip = entry / "clip.mp4"
    if manifest is None or entry.is_symlink() or not entry.is_dir() or clip.is_symlink() or not clip.is_file():
        return None
    if manifest.get("schema_version") != CACHE_SCHEMA_VERSION or manifest.get("kind") != "prepared-live":
        return None
    binding = manifest.get("binding")
    if not isinstance(binding, dict) or manifest.get("fingerprint") != _binding_hash(binding):
        return None
    if fingerprint != manifest.get("fingerprint"):
        return None
    if expected_binding is not None and binding != expected_binding:
        return None
    # A bridge/M8 code change invalidates old prepared derivatives even during cache sanitization.
    if binding.get("bridge_sha256") != _module_sha(sys.modules[__name__]):
        return None
    if binding.get("m8_live_sha256") != _module_sha(m8_live):
        return None
    size = clip.stat().st_size
    if size <= 0 or size > MAX_CLIP_BYTES or manifest.get("byte_length") != size:
        return None
    sha = str(manifest.get("sha256") or "")
    if not sha or _sha256_file(clip) != sha:
        return None
    try:
        sidecar = _validate_sidecar(entry, manifest)
    except RuntimeError:
        return None
    return clip, sidecar, manifest


def _restore(root: Path, fingerprint: str, binding: dict[str, Any], dest: Path, seconds: float) -> Path | None:
    valid = _validate_entry(root, fingerprint, binding)
    if valid is None:
        return None
    clip, sidecar, _manifest = valid
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        _atomic_copy(clip, dest)
        if sidecar is not None:
            _atomic_copy(sidecar, _sidecar_source(dest))
        rendered_seconds = float(orchestrator.duration(dest))
        target = float(seconds)
        if rendered_seconds + 0.25 < target or rendered_seconds > target + 1.0:
            raise RuntimeError("media_prepared_live_duration_mismatch")
        print(f"Media prepared live HIT fingerprint={fingerprint[:12]}")
        return dest
    except Exception as exc:
        dest.unlink(missing_ok=True)
        _sidecar_source(dest).unlink(missing_ok=True)
        print(f"Media prepared live restore rejected ({type(exc).__name__}); rerendering")
        return None


def _persist(root: Path, fingerprint: str, binding: dict[str, Any], result: Path) -> None:
    result = Path(result)
    if result.is_symlink() or not result.is_file():
        return
    size = result.stat().st_size
    if size <= 0 or size > MAX_CLIP_BYTES:
        return
    entry = _entry(root, fingerprint)
    clip = entry / "clip.mp4"
    _atomic_copy(result, clip)
    manifest: dict[str, Any] = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "kind": "prepared-live",
        "fingerprint": fingerprint,
        "binding": binding,
        "sha256": _sha256_file(clip),
        "byte_length": clip.stat().st_size,
    }
    sidecar = _sidecar_source(result)
    if sidecar.is_file() and not sidecar.is_symlink():
        sidecar_size = sidecar.stat().st_size
        if 0 < sidecar_size <= MAX_SIDECAR_BYTES and _load_json(sidecar) is not None:
            cached_sidecar = entry / "clip.m8.json"
            _atomic_copy(sidecar, cached_sidecar)
            manifest["m8_sidecar"] = {
                "filename": cached_sidecar.name,
                "sha256": _sha256_file(cached_sidecar),
                "byte_length": cached_sidecar.stat().st_size,
            }
    _atomic_json(entry / "manifest.json", manifest)


def _wrap_prepare_clip(root: Path, original: Callable[..., Path]) -> Callable[..., Path]:
    def wrapped(src: Path, dest: Path, seconds: float, portrait: bool, fps: int = 30) -> Path:
        src = Path(src)
        dest = Path(dest)
        record = trust.trusted_record(src)
        if record is None:
            return original(src, dest, seconds, portrait, fps=fps)
        binding = _binding(record, original, seconds=seconds, portrait=portrait, fps=fps)
        fingerprint = _binding_hash(binding)
        restored = _restore(root, fingerprint, binding, dest, seconds)
        if restored is not None:
            return restored
        result = Path(original(src, dest, seconds, portrait, fps=fps))
        try:
            _persist(root, fingerprint, binding, result)
        except Exception as exc:
            print(f"Media prepared live persistence skipped ({type(exc).__name__})")
        return result

    wrapped._isco_media_prepared_live = True
    wrapped._isco_media_prepared_live_original = original
    return wrapped


@contextmanager
def media_prepared_cache_scope() -> Iterator[None]:
    """Wrap the prepare_clip seam after outer live render scopes (notably M8) are active."""
    root = _cache_root()
    current = orchestrator.prepare_clip
    if root is None or getattr(current, "_isco_media_prepared_live", False):
        yield
        return
    # When no outer live renderer replaced the original Media wrapper, do not double-cache.
    if getattr(current, "_isco_media_durable_prepare", False):
        yield
        return
    root.mkdir(parents=True, exist_ok=True)
    wrapped = _wrap_prepare_clip(root, current)
    orchestrator.prepare_clip = wrapped
    try:
        yield
    finally:
        if orchestrator.prepare_clip is wrapped:
            orchestrator.prepare_clip = current


def install_media_prepared_live_cache() -> None:
    """Bind prepared-clip durability inside M8's live scope without changing M8 itself."""
    global _INSTALLED, _ORIGINAL_PRODUCE
    if _INSTALLED:
        return
    if _cache_root() is None:
        print("Media prepared live cache disabled: durable stage cache not configured")
        return
    current = orchestrator.produce
    if getattr(current, "_isco_media_prepared_live_produce", False):
        _INSTALLED = True
        return

    _ORIGINAL_PRODUCE = current

    def wrapped(*args, **kwargs):
        with media_prepared_cache_scope():
            return current(*args, **kwargs)

    wrapped._isco_media_prepared_live_produce = True
    wrapped._isco_media_prepared_live_original = current
    orchestrator.produce = wrapped
    _INSTALLED = True
    print("Media prepared live cache installed: M8-composed prepared derivatives are durable")


def reset_media_prepared_live_cache_for_tests() -> None:
    global _INSTALLED, _ORIGINAL_PRODUCE
    if _ORIGINAL_PRODUCE is not None and getattr(
        orchestrator.produce, "_isco_media_prepared_live_produce", False
    ):
        orchestrator.produce = _ORIGINAL_PRODUCE
    _ORIGINAL_PRODUCE = None
    _INSTALLED = False


def _remove_path(path: Path) -> None:
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    except OSError:
        pass


def prepare_cache_for_persistence(media_root: Path) -> bool:
    """Validate/cap only the M8-composed prepared namespace inside the shared Media root."""
    root = Path(media_root) / "prepared-live"
    if not root.exists():
        return False
    if root.is_symlink() or not root.is_dir():
        _remove_path(root)
        return False

    valid: list[tuple[float, Path, int]] = []
    for entry in list(root.iterdir()):
        manifest = _load_json(entry / "manifest.json") if entry.is_dir() and not entry.is_symlink() else None
        fingerprint = str(manifest.get("fingerprint") or "") if manifest else ""
        checked = _validate_entry(root, fingerprint) if fingerprint else None
        if checked is None:
            _remove_path(entry)
            continue
        clip, sidecar, _manifest = checked
        total = clip.stat().st_size + (sidecar.stat().st_size if sidecar is not None else 0)
        valid.append((entry.stat().st_mtime, entry, total))

    if len(valid) > MAX_ENTRIES:
        excess = len(valid) - MAX_ENTRIES
        for _mtime, entry, _size in sorted(valid)[:excess]:
            _remove_path(entry)
        valid = sorted(valid)[excess:]

    total = sum(size for _mtime, _entry, size in valid)
    if total > MAX_TOTAL_BYTES:
        kept = list(sorted(valid))
        for item in list(kept):
            if total <= MAX_TOTAL_BYTES:
                break
            _mtime, entry, size = item
            _remove_path(entry)
            total -= size
            kept.remove(item)
        valid = kept

    remaining = any(entry.exists() for _mtime, entry, _size in valid)
    print(f"Media prepared live cache sanitized: save_allowed={remaining}")
    return remaining

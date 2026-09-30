from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
import wave
from pathlib import Path
from typing import Any, Mapping


CACHE_SCHEMA_VERSION = 1
CACHE_NAMESPACE = "clean-v2-gemini38-chunk-v1"
AUDIO_FILENAME = "chunk.wav"
MANIFEST_FILENAME = "manifest.json"
MIN_AUDIO_BYTES = 1024
MAX_AUDIO_BYTES = 256 * 1024 * 1024
MAX_PERSISTED_ENTRIES = 128
MAX_PERSISTED_BYTES = 768 * 1024 * 1024


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _cache_root() -> Path | None:
    raw = str(os.environ.get("CLEAN_V2_TTS_CACHE_PATH") or "").strip()
    return Path(raw) if raw else None


def build_binding(
    *,
    transcript: str,
    model: str,
    primary_voice: str,
    questioner_voice: str,
    performance_mode: str,
    provider: str,
) -> dict[str, Any]:
    """Bind cached audio to every input that can change the spoken result.

    Raw narration is deliberately not persisted in the cache manifest. Its digest
    is sufficient to make reuse exact while the WAV remains the only content file.
    """
    return {
        "cache_schema_version": CACHE_SCHEMA_VERSION,
        "cache_namespace": CACHE_NAMESPACE,
        "transcript_sha256": _sha256_text(str(transcript)),
        "model": str(model),
        "primary_voice": str(primary_voice),
        "questioner_voice": str(questioner_voice),
        "performance_mode": str(performance_mode or ""),
        "provider": str(provider),
    }


def _fingerprint(binding: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(binding),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256_bytes(encoded)


def _entries_root(root: Path) -> Path:
    return root / CACHE_NAMESPACE / "entries"


def _entry_dir(root: Path, fingerprint: str) -> Path:
    if len(fingerprint) != 64 or any(
        ch not in "0123456789abcdef" for ch in fingerprint
    ):
        raise RuntimeError("clean_v2_tts_cache_invalid_fingerprint")
    entries = _entries_root(root)
    entry = entries / fingerprint
    if entry.resolve(strict=False).parent != entries.resolve(strict=False):
        raise RuntimeError("clean_v2_tts_cache_path_escape")
    return entry


def _valid_wav(path: Path) -> bool:
    try:
        if path.is_symlink() or not path.is_file():
            return False
        size = path.stat().st_size
        if size < MIN_AUDIO_BYTES or size > MAX_AUDIO_BYTES:
            return False
        with wave.open(str(path), "rb") as wav:
            return (
                wav.getnchannels() == 1
                and wav.getsampwidth() == 2
                and wav.getframerate() == 24000
                and wav.getnframes() > 0
            )
    except (OSError, EOFError, wave.Error):
        return False


def _load_manifest(path: Path) -> dict[str, Any] | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _validate_entry(
    root: Path,
    fingerprint: str,
    *,
    binding: Mapping[str, Any] | None = None,
) -> tuple[Path, dict[str, Any]] | None:
    try:
        entry = _entry_dir(root, fingerprint)
    except RuntimeError:
        return None
    if entry.is_symlink() or not entry.is_dir():
        return None
    audio = entry / AUDIO_FILENAME
    manifest = _load_manifest(entry / MANIFEST_FILENAME)
    if manifest is None or not _valid_wav(audio):
        return None
    if manifest.get("schema_version") != CACHE_SCHEMA_VERSION:
        return None
    if manifest.get("fingerprint") != fingerprint:
        return None
    if binding is not None and manifest.get("binding") != dict(binding):
        return None
    try:
        size = audio.stat().st_size
    except OSError:
        return None
    if manifest.get("audio_bytes") != size:
        return None
    if manifest.get("audio_sha256") != _sha256_file(audio):
        return None
    return audio, manifest


def _remove_entry(path: Path) -> None:
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    except OSError:
        pass


def restore(binding: Mapping[str, Any], destination: Path) -> bool:
    root = _cache_root()
    if root is None or root.is_symlink():
        return False
    fingerprint = _fingerprint(binding)
    valid = _validate_entry(root, fingerprint, binding=binding)
    if valid is None:
        return False
    audio, _manifest = valid
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.clean-v2-cache.tmp")
    try:
        shutil.copyfile(audio, temporary)
        if not _valid_wav(temporary):
            raise RuntimeError("clean_v2_tts_cache_restore_invalid_wav")
        os.replace(temporary, destination)
        os.utime(audio, None)
    except Exception:
        temporary.unlink(missing_ok=True)
        destination.unlink(missing_ok=True)
        _remove_entry(audio.parent)
        return False
    print(f"Clean V2 TTS cache HIT fingerprint={fingerprint[:12]}")
    return True


def persist(binding: Mapping[str, Any], source: Path) -> bool:
    root = _cache_root()
    source = Path(source)
    if root is None or root.is_symlink() or not _valid_wav(source):
        return False
    fingerprint = _fingerprint(binding)
    entry = _entry_dir(root, fingerprint)
    entry.parent.mkdir(parents=True, exist_ok=True)
    if _validate_entry(root, fingerprint, binding=binding) is not None:
        return True

    temporary = Path(tempfile.mkdtemp(prefix=f".{fingerprint[:12]}-", dir=entry.parent))
    try:
        cached_audio = temporary / AUDIO_FILENAME
        shutil.copyfile(source, cached_audio)
        if not _valid_wav(cached_audio):
            raise RuntimeError("clean_v2_tts_cache_persist_invalid_wav")
        manifest = {
            "schema_version": CACHE_SCHEMA_VERSION,
            "fingerprint": fingerprint,
            "binding": dict(binding),
            "audio_sha256": _sha256_file(cached_audio),
            "audio_bytes": cached_audio.stat().st_size,
        }
        (temporary / MANIFEST_FILENAME).write_text(
            json.dumps(manifest, ensure_ascii=True, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        if entry.exists():
            _remove_entry(entry)
        os.replace(temporary, entry)
    except Exception:
        _remove_entry(temporary)
        return False
    print(f"Clean V2 TTS cache SAVE fingerprint={fingerprint[:12]}")
    return True


def prepare_cache_for_persistence(root: Path) -> bool:
    """Keep only verified, bounded entries before GitHub Actions saves the cache."""
    root = Path(root)
    if root.is_symlink():
        _remove_entry(root)
        return False
    entries = _entries_root(root)
    if entries.is_symlink():
        _remove_entry(entries)
        return False
    if not entries.is_dir():
        return False

    valid: list[tuple[float, int, Path]] = []
    for entry in list(entries.iterdir()):
        if entry.is_symlink() or not entry.is_dir() or len(entry.name) != 64:
            _remove_entry(entry)
            continue
        checked = _validate_entry(root, entry.name)
        if checked is None:
            _remove_entry(entry)
            continue
        audio, _manifest = checked
        try:
            valid.append((audio.stat().st_mtime, audio.stat().st_size, entry))
        except OSError:
            _remove_entry(entry)

    valid.sort(key=lambda item: item[0], reverse=True)
    kept = 0
    kept_bytes = 0
    for _mtime, size, entry in valid:
        if kept >= MAX_PERSISTED_ENTRIES or kept_bytes + size > MAX_PERSISTED_BYTES:
            _remove_entry(entry)
            continue
        kept += 1
        kept_bytes += size
    print(f"Clean V2 TTS cache prepared entries={kept} bytes={kept_bytes}")
    return kept > 0


def _main() -> int:
    parser = argparse.ArgumentParser(description="Prepare Clean V2 TTS chunk cache")
    parser.add_argument("command", choices=("prepare",))
    parser.add_argument("--root", required=True)
    args = parser.parse_args()
    allowed = prepare_cache_for_persistence(Path(args.root))
    print(f"save_allowed={'true' if allowed else 'false'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())

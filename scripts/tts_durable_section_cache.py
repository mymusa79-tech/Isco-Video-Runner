from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

import isco_video_agent.orchestrator as orchestrator

from scripts import voice_mesh


SCHEMA_VERSION = 1
CACHE_NAMESPACE = "tts-section-v1"
AUDIT_FILENAME = "tts-durable-cache-audit.json"
CACHE_AUDIO_FILENAME = "audio.wav"
CACHE_MANIFEST_FILENAME = "manifest.json"
MAX_CACHE_ENTRIES = 24
MAX_CACHE_BYTES = 256 * 1024 * 1024

_original_synthesize_tts_section: Callable[..., Any] | None = None


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


def _voice_contract_sha256() -> str:
    """Bind cache compatibility to the executable Voice Mesh, not unrelated Runner code."""
    source = Path(voice_mesh.__file__).resolve()
    return _sha256_file(source)


def _dialogue_mode() -> bool:
    return str(os.environ.get("ISCO_DIALOGUE_QA") or "").strip() == "1"


def _qa_text(transcript: str, *, dialogue: bool) -> str:
    if dialogue:
        return voice_mesh._DIALOGUE_LABEL.sub("", transcript)
    return transcript


def semantic_fingerprint(
    *,
    transcript: str,
    model: str,
    voice: str,
    style: str,
    dialogue: bool | None = None,
) -> tuple[str, dict[str, Any]]:
    dialogue_mode = _dialogue_mode() if dialogue is None else bool(dialogue)
    transcript_bytes = str(transcript).encode("utf-8")
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "namespace": CACHE_NAMESPACE,
        "transcript_sha256": _sha256_bytes(transcript_bytes),
        "transcript_utf8_bytes": len(transcript_bytes),
        "model": str(model),
        "voice": str(voice),
        "style": str(style),
        "dialogue_mode": dialogue_mode,
        "voice_contract_sha256": _voice_contract_sha256(),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _sha256_bytes(canonical), payload


def _configured_cache_root() -> Path | None:
    raw = str(os.environ.get("ISCO_TTS_CACHE_DIR") or "").strip()
    if not raw:
        return None
    root = Path(raw)
    if root.exists() and root.is_symlink():
        raise CacheEntryInvalid("tts_cache_root_symlink_rejected")
    root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir():
        raise CacheEntryInvalid("tts_cache_root_not_directory")
    return root.resolve()


def _entry_dir(root: Path, fingerprint: str) -> Path:
    if len(fingerprint) != 64 or any(ch not in "0123456789abcdef" for ch in fingerprint):
        raise CacheEntryInvalid("tts_cache_fingerprint_invalid")
    entry = root / fingerprint
    try:
        if entry.parent.resolve() != root.resolve():
            raise CacheEntryInvalid("tts_cache_path_escape")
    except OSError as exc:
        raise CacheEntryInvalid("tts_cache_path_resolution_failed") from exc
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


def _read_manifest(entry: Path, fingerprint: str) -> tuple[dict[str, Any], Path]:
    if entry.is_symlink():
        raise CacheEntryInvalid("tts_cache_entry_symlink_rejected")
    manifest_path = entry / CACHE_MANIFEST_FILENAME
    audio_path = entry / CACHE_AUDIO_FILENAME
    if manifest_path.is_symlink() or audio_path.is_symlink():
        raise CacheEntryInvalid("tts_cache_file_symlink_rejected")
    if not manifest_path.is_file() or not audio_path.is_file():
        raise CacheEntryInvalid("tts_cache_entry_incomplete")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise CacheEntryInvalid("tts_cache_manifest_invalid_json") from exc
    if not isinstance(manifest, dict):
        raise CacheEntryInvalid("tts_cache_manifest_not_object")
    if manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("namespace") != CACHE_NAMESPACE:
        raise CacheEntryInvalid("tts_cache_manifest_version_mismatch")
    if manifest.get("fingerprint") != fingerprint:
        raise CacheEntryInvalid("tts_cache_manifest_fingerprint_mismatch")
    expected_size = manifest.get("audio_bytes")
    expected_sha = manifest.get("audio_sha256")
    if not isinstance(expected_size, int) or expected_size <= 0:
        raise CacheEntryInvalid("tts_cache_manifest_size_invalid")
    if not isinstance(expected_sha, str) or len(expected_sha) != 64:
        raise CacheEntryInvalid("tts_cache_manifest_sha_invalid")
    if audio_path.stat().st_size != expected_size:
        raise CacheEntryInvalid("tts_cache_audio_size_mismatch")
    if _sha256_file(audio_path) != expected_sha:
        raise CacheEntryInvalid("tts_cache_audio_sha_mismatch")
    return manifest, audio_path


def _evict_entry(entry: Path) -> None:
    try:
        if entry.is_symlink():
            entry.unlink(missing_ok=True)
        elif entry.exists():
            shutil.rmtree(entry)
    except OSError:
        pass


def _append_audit(output: Path, event: dict[str, Any]) -> None:
    try:
        audit_path = output.parent.parent / AUDIT_FILENAME
        if audit_path.is_file():
            document = json.loads(audit_path.read_text(encoding="utf-8"))
        else:
            document = {"schema_version": 1, "mode": "semantic_section_resume", "events": []}
        if not isinstance(document, dict) or not isinstance(document.get("events"), list):
            document = {"schema_version": 1, "mode": "semantic_section_resume", "events": []}
        document["events"].append(event)
        document["summary"] = {
            "hits": sum(1 for item in document["events"] if item.get("outcome") == "hit"),
            "misses": sum(1 for item in document["events"] if item.get("outcome") == "miss_stored"),
            "invalidated": sum(1 for item in document["events"] if item.get("outcome") == "invalidated"),
            "synthesis_failures": sum(1 for item in document["events"] if item.get("outcome") == "synthesis_failed"),
        }
        _atomic_json(audit_path, document)
    except Exception as exc:
        print(f"TTS durable cache audit skipped ({type(exc).__name__})")


def _restore_cached_audio(
    *,
    root: Path,
    fingerprint: str,
    transcript: str,
    output: Path,
    dialogue: bool,
) -> dict[str, Any] | None:
    entry = _entry_dir(root, fingerprint)
    if not entry.exists():
        return None
    manifest, cached_audio = _read_manifest(entry, fingerprint)
    _atomic_copy(cached_audio, output)
    try:
        voice_mesh._qa(output, _qa_text(transcript, dialogue=dialogue))
    except Exception as exc:
        output.unlink(missing_ok=True)
        raise CacheEntryInvalid(f"tts_cache_restored_voice_qa_failed:{type(exc).__name__}") from exc
    if _sha256_file(output) != manifest["audio_sha256"]:
        output.unlink(missing_ok=True)
        raise CacheEntryInvalid("tts_cache_restored_output_sha_mismatch")
    provenance = manifest.get("provenance") if isinstance(manifest.get("provenance"), dict) else {}
    provider = str(provenance.get("provider") or "unknown")
    fallback_used = provenance.get("fallback_used")
    voice_mesh._record_voice_provenance(
        output,
        provider=provider,
        fallback_used=bool(fallback_used) if fallback_used is not None else False,
    )
    try:
        os.utime(entry, None)
    except OSError:
        pass
    return manifest


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
    try:
        for path in root.iterdir():
            if path.is_symlink():
                path.unlink(missing_ok=True)
                continue
            if not path.is_dir():
                continue
            size = _entry_size(path)
            try:
                stamp = path.stat().st_mtime
            except OSError:
                stamp = 0.0
            entries.append((stamp, path, size))
    except OSError:
        return
    entries.sort(key=lambda item: item[0], reverse=True)
    kept = 0
    used = 0
    for _stamp, path, size in entries:
        if kept < MAX_CACHE_ENTRIES and used + size <= MAX_CACHE_BYTES:
            kept += 1
            used += size
            continue
        _evict_entry(path)


def _store_cached_audio(
    *,
    root: Path,
    fingerprint: str,
    semantic_payload: dict[str, Any],
    transcript: str,
    output: Path,
    dialogue: bool,
) -> dict[str, Any]:
    if output.is_symlink() or not output.is_file() or output.stat().st_size <= 0:
        raise CacheEntryInvalid("tts_cache_source_audio_invalid")
    voice_mesh._qa(output, _qa_text(transcript, dialogue=dialogue))
    provenance = voice_mesh._voice_provenance.get(
        voice_mesh._output_key(output),
        {"provider": "unknown", "fallback_used": None},
    )
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "namespace": CACHE_NAMESPACE,
        "fingerprint": fingerprint,
        "semantic_contract": semantic_payload,
        "audio_sha256": _sha256_file(output),
        "audio_bytes": output.stat().st_size,
        "provenance": {
            "provider": str(provenance.get("provider") or "unknown"),
            "fallback_used": provenance.get("fallback_used"),
        },
        "created_unix": int(time.time()),
    }
    entry = _entry_dir(root, fingerprint)
    tmp = Path(tempfile.mkdtemp(prefix=f".{fingerprint[:12]}-", dir=root))
    try:
        shutil.copyfile(output, tmp / CACHE_AUDIO_FILENAME)
        _atomic_json(tmp / CACHE_MANIFEST_FILENAME, manifest)
        _evict_entry(entry)
        os.replace(tmp, entry)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    _prune_cache(root)
    return manifest


def prepare_cache_for_persistence(path: Path | None = None) -> bool:
    """Sanitize the bounded rebuildable cache before Actions cache persistence."""
    try:
        root = Path(path).resolve() if path is not None else _configured_cache_root()
        if root is None or not root.exists() or root.is_symlink():
            return False
        _prune_cache(root)
        valid = 0
        for entry in list(root.iterdir()):
            if entry.is_symlink() or not entry.is_dir():
                _evict_entry(entry)
                continue
            fingerprint = entry.name
            try:
                _read_manifest(entry, fingerprint)
            except Exception:
                _evict_entry(entry)
                continue
            valid += 1
        print(f"TTS durable cache sanitized for persistence: valid_entries={valid}")
        return valid > 0
    except Exception as exc:
        print(f"TTS durable cache persistence skipped ({type(exc).__name__})")
        return False


def install_tts_durable_section_cache() -> None:
    """Resume successful TTS sections across runs without changing provider retry ownership."""
    global _original_synthesize_tts_section
    current = orchestrator._synthesize_tts_section
    if getattr(current, "_isco_tts_durable_section_cache", False) is True:
        return
    _original_synthesize_tts_section = current

    def wrapped(
        ledger,
        circuit,
        budget,
        *,
        task_id: str,
        api_key: str,
        transcript: str,
        output: Path,
        model: str,
        voice: str,
        style: str,
    ) -> Path:
        if not str(task_id).startswith("TTS_SECTION_"):
            return _original_synthesize_tts_section(
                ledger,
                circuit,
                budget,
                task_id=task_id,
                api_key=api_key,
                transcript=transcript,
                output=output,
                model=model,
                voice=voice,
                style=style,
            )
        try:
            root = _configured_cache_root()
        except Exception as exc:
            root = None
            print(f"TTS durable cache disabled for this call ({type(exc).__name__})")
        if root is None:
            return _original_synthesize_tts_section(
                ledger,
                circuit,
                budget,
                task_id=task_id,
                api_key=api_key,
                transcript=transcript,
                output=output,
                model=model,
                voice=voice,
                style=style,
            )

        dialogue = _dialogue_mode()
        fingerprint, semantic_payload = semantic_fingerprint(
            transcript=transcript,
            model=model,
            voice=voice,
            style=style,
            dialogue=dialogue,
        )
        entry = _entry_dir(root, fingerprint)
        try:
            manifest = _restore_cached_audio(
                root=root,
                fingerprint=fingerprint,
                transcript=transcript,
                output=Path(output),
                dialogue=dialogue,
            )
            if manifest is not None:
                provider = str((manifest.get("provenance") or {}).get("provider") or "unknown")
                _append_audit(Path(output), {
                    "task_id": task_id,
                    "fingerprint": fingerprint,
                    "outcome": "hit",
                    "source_provider": provider,
                    "audio_sha256": manifest.get("audio_sha256"),
                })
                print(f"TTS durable cache HIT: {task_id} source_provider={provider}")
                return Path(output)
        except Exception as exc:
            _evict_entry(entry)
            _append_audit(Path(output), {
                "task_id": task_id,
                "fingerprint": fingerprint,
                "outcome": "invalidated",
                "reason": type(exc).__name__,
            })
            print(f"TTS durable cache invalidated: {task_id} ({type(exc).__name__})")

        try:
            result = _original_synthesize_tts_section(
                ledger,
                circuit,
                budget,
                task_id=task_id,
                api_key=api_key,
                transcript=transcript,
                output=output,
                model=model,
                voice=voice,
                style=style,
            )
        except Exception as exc:
            _append_audit(Path(output), {
                "task_id": task_id,
                "fingerprint": fingerprint,
                "outcome": "synthesis_failed",
                "reason": type(exc).__name__,
            })
            raise

        result_path = Path(result if result is not None else output)
        try:
            manifest = _store_cached_audio(
                root=root,
                fingerprint=fingerprint,
                semantic_payload=semantic_payload,
                transcript=transcript,
                output=result_path,
                dialogue=dialogue,
            )
            _append_audit(result_path, {
                "task_id": task_id,
                "fingerprint": fingerprint,
                "outcome": "miss_stored",
                "source_provider": (manifest.get("provenance") or {}).get("provider"),
                "audio_sha256": manifest.get("audio_sha256"),
            })
            print(f"TTS durable cache STORED: {task_id}")
        except Exception as exc:
            # Cache persistence is an optimization; a valid freshly synthesized section
            # must not become a production failure because local cache storage failed.
            print(f"TTS durable cache store skipped: {task_id} ({type(exc).__name__})")
        return result_path

    wrapped._isco_tts_durable_section_cache = True
    wrapped._isco_tts_durable_original = current
    orchestrator._synthesize_tts_section = wrapped
    print("TTS durable section cache installed: semantic fingerprint + SHA256 + Voice QA resume")

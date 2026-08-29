from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

import isco_video_agent.orchestrator as orchestrator

from scripts import voice_mesh

CACHE_SCHEMA_VERSION = 1
CACHE_NAMESPACE = "tts-section-v1"
AUDIO_FILENAME = "section.wav"
MANIFEST_FILENAME = "manifest.json"
MIN_AUDIO_BYTES = 1024
MAX_AUDIO_BYTES = 256 * 1024 * 1024
MAX_PERSISTED_ENTRIES = 16


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_file_hash(path: Path) -> str | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        return _sha256_file(path)
    except OSError:
        return None


def _required_env(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise RuntimeError(f"tts_cache_missing_contract_env:{name}")
    return value


def _cache_root() -> Path | None:
    raw = (os.environ.get("ISCO_TTS_CACHE_PATH") or "").strip()
    return Path(raw) if raw else None


def _piper_hashes() -> tuple[str | None, str | None]:
    raw = (os.environ.get("PIPER_MODEL_PATH") or "").strip()
    if not raw:
        return None, None
    model = Path(raw)
    return _regular_file_hash(model), _regular_file_hash(Path(str(model) + ".json"))


def _module_hash(module) -> str:
    path = Path(module.__file__).resolve()
    return _sha256_file(path)


def _binding(
    *,
    task_id: str,
    transcript: str,
    model: str,
    voice: str,
    style: str,
) -> dict[str, Any]:
    piper_model_sha256, piper_config_sha256 = _piper_hashes()
    return {
        "cache_namespace": CACHE_NAMESPACE,
        "cache_schema_version": CACHE_SCHEMA_VERSION,
        "approved_brief_sha256": _required_env("ISCO_APPROVED_BRIEF_SHA256"),
        "engine_sha": _required_env("ISCO_ENGINE_SHA"),
        "task_id": str(task_id),
        "transcript_sha256": _sha256_text(str(transcript)),
        "tts_model": str(model),
        "requested_voice": str(voice),
        "style_sha256": _sha256_text(str(style)),
        "dialogue_mode": os.environ.get("ISCO_DIALOGUE_QA") == "1",
        "voice_mesh_sha256": _module_hash(voice_mesh),
        "cache_contract_sha256": _module_hash(__import__(__name__, fromlist=["*"])),
        "piper_model_sha256": piper_model_sha256,
        "piper_config_sha256": piper_config_sha256,
    }


def _fingerprint(binding: dict[str, Any]) -> str:
    encoded = json.dumps(binding, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return _sha256_bytes(encoded)


def _entry_dir(root: Path, fingerprint: str) -> Path:
    return root / "entries" / fingerprint


def _safe_entry_dir(root: Path, fingerprint: str) -> Path:
    entry = _entry_dir(root, fingerprint)
    root_resolved = root.resolve()
    entry_resolved = entry.resolve(strict=False)
    if entry_resolved.parent.parent != root_resolved:
        raise RuntimeError("tts_cache_path_escape")
    return entry


def _load_manifest(path: Path) -> dict[str, Any] | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _validate_entry(
    root: Path,
    *,
    fingerprint: str,
    binding: dict[str, Any] | None = None,
) -> tuple[Path, dict[str, Any]] | None:
    try:
        entry = _safe_entry_dir(root, fingerprint)
    except RuntimeError:
        return None
    if entry.is_symlink() or not entry.is_dir():
        return None
    manifest = _load_manifest(entry / MANIFEST_FILENAME)
    audio = entry / AUDIO_FILENAME
    if manifest is None or audio.is_symlink() or not audio.is_file():
        return None
    if manifest.get("schema_version") != CACHE_SCHEMA_VERSION:
        return None
    if manifest.get("fingerprint") != fingerprint:
        return None
    if binding is not None and manifest.get("binding") != binding:
        return None
    try:
        size = audio.stat().st_size
    except OSError:
        return None
    if size < MIN_AUDIO_BYTES or size > MAX_AUDIO_BYTES:
        return None
    if manifest.get("audio_bytes") != size:
        return None
    expected_sha = manifest.get("audio_sha256")
    if not isinstance(expected_sha, str) or expected_sha != _sha256_file(audio):
        return None
    provenance = manifest.get("provenance")
    if not isinstance(provenance, dict):
        return None
    provider = provenance.get("provider")
    fallback_used = provenance.get("fallback_used")
    if not isinstance(provider, str) or not provider or provider == "unknown":
        return None
    if not isinstance(fallback_used, bool):
        return None
    return audio, manifest


def _invalidate_entry(entry: Path) -> None:
    try:
        shutil.rmtree(entry)
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(f"TTS durable cache invalid-entry cleanup skipped ({type(exc).__name__})")


def _restore(
    root: Path,
    *,
    fingerprint: str,
    binding: dict[str, Any],
    transcript: str,
    output: Path,
) -> bool:
    valid = _validate_entry(root, fingerprint=fingerprint, binding=binding)
    if valid is None:
        return False
    audio, manifest = valid
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tts-cache-restore.tmp")
    try:
        shutil.copyfile(audio, temporary)
        os.replace(temporary, output)
        voice_mesh.qa_voice_output(output, transcript)
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        output.unlink(missing_ok=True)
        _invalidate_entry(audio.parent)
        print(f"TTS durable cache rejected {fingerprint[:12]} ({type(exc).__name__}); regenerating")
        return False

    provenance = manifest["provenance"]
    voice_mesh.record_voice_provenance(
        output,
        provider=f"durable-cache:{provenance['provider']}",
        fallback_used=bool(provenance["fallback_used"]),
    )
    print(f"TTS durable cache HIT task={binding['task_id']} fingerprint={fingerprint[:12]}")
    return True


def _persist(
    root: Path,
    *,
    fingerprint: str,
    binding: dict[str, Any],
    output: Path,
) -> bool:
    provenance = voice_mesh.peek_voice_provenance(output)
    provider = provenance.get("provider")
    fallback_used = provenance.get("fallback_used")
    if not isinstance(provider, str) or not provider or provider == "unknown" or not isinstance(fallback_used, bool):
        print(f"TTS durable cache skip task={binding['task_id']}: missing trusted voice provenance")
        return False
    if output.is_symlink() or not output.is_file():
        return False
    size = output.stat().st_size
    if size < MIN_AUDIO_BYTES or size > MAX_AUDIO_BYTES:
        return False

    entry = _safe_entry_dir(root, fingerprint)
    entry.mkdir(parents=True, exist_ok=True)
    audio = entry / AUDIO_FILENAME
    manifest_path = entry / MANIFEST_FILENAME
    audio_tmp = entry / (AUDIO_FILENAME + ".tmp")
    manifest_tmp = entry / (MANIFEST_FILENAME + ".tmp")
    try:
        shutil.copyfile(output, audio_tmp)
        os.replace(audio_tmp, audio)
        manifest = {
            "schema_version": CACHE_SCHEMA_VERSION,
            "fingerprint": fingerprint,
            "binding": binding,
            "audio_sha256": _sha256_file(audio),
            "audio_bytes": audio.stat().st_size,
            "provenance": {
                "provider": provider,
                "fallback_used": fallback_used,
            },
        }
        manifest_tmp.write_text(json.dumps(manifest, sort_keys=True, indent=2), encoding="utf-8")
        os.replace(manifest_tmp, manifest_path)
    except Exception as exc:
        audio_tmp.unlink(missing_ok=True)
        manifest_tmp.unlink(missing_ok=True)
        _invalidate_entry(entry)
        print(f"TTS durable cache persistence skipped ({type(exc).__name__})")
        return False
    print(f"TTS durable cache SAVE task={binding['task_id']} fingerprint={fingerprint[:12]}")
    return True


def install_tts_durable_cache() -> None:
    """Wrap the Engine's common section TTS boundary with durable content-addressed reuse.

    The wrapper performs no provider retries and is installed after Voice Mesh but before
    Voice Identity Observer. A valid cache hit therefore skips provider/ledger work while
    still passing through Voice QA here and Voice Identity Observer outside this wrapper.
    """
    root = _cache_root()
    if root is None:
        print("TTS durable cache disabled: ISCO_TTS_CACHE_PATH not configured")
        return
    root.mkdir(parents=True, exist_ok=True)
    current = orchestrator._synthesize_tts_section
    if getattr(current, "_is_tts_durable_cache", False) is True:
        return
    original = current

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
        output = Path(output)
        binding = _binding(
            task_id=task_id,
            transcript=transcript,
            model=model,
            voice=voice,
            style=style,
        )
        fingerprint = _fingerprint(binding)
        if _restore(
            root,
            fingerprint=fingerprint,
            binding=binding,
            transcript=transcript,
            output=output,
        ):
            return output

        result = original(
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
        result_path = Path(result)
        try:
            _persist(
                root,
                fingerprint=fingerprint,
                binding=binding,
                output=result_path,
            )
        except Exception as exc:
            print(f"TTS durable cache write skipped ({type(exc).__name__}); production unchanged")
        return result_path

    wrapped._is_tts_durable_cache = True
    wrapped._tts_durable_cache_original = original
    orchestrator._synthesize_tts_section = wrapped
    print("TTS durable cache installed: per-section semantic reuse, SHA/size/QA verified, no retry ownership")


def prepare_cache_for_persistence(root: Path) -> bool:
    """Sanitize the shared durable-stage envelope before Actions cache persistence.

    TTS keeps ownership of the historical Actions restore/save envelope. Stacked stages
    may add independent namespaces under that root; each namespace sanitizes itself and
    only a valid namespace can make the envelope persist. This keeps one bounded cache
    transport while preserving separate TTS and Media semantic authorities.
    """
    root = Path(root)
    entries_root = root / "entries"
    valid: list[tuple[float, Path]] = []
    if entries_root.is_symlink():
        _invalidate_entry(entries_root)
    elif entries_root.is_dir():
        for entry in list(entries_root.iterdir()):
            if entry.is_symlink() or not entry.is_dir() or len(entry.name) != 64:
                _invalidate_entry(entry)
                continue
            checked = _validate_entry(root, fingerprint=entry.name)
            if checked is None:
                _invalidate_entry(entry)
                continue
            try:
                valid.append((entry.stat().st_mtime, entry))
            except OSError:
                _invalidate_entry(entry)

    if len(valid) > MAX_PERSISTED_ENTRIES:
        for _, entry in sorted(valid)[: len(valid) - MAX_PERSISTED_ENTRIES]:
            _invalidate_entry(entry)
        valid = sorted(valid)[-MAX_PERSISTED_ENTRIES:]

    media_valid = False
    media_root = root / "media"
    if media_root.exists():
        try:
            from scripts.media_durable_cache import prepare_cache_for_persistence as prepare_media_cache

            media_valid = bool(prepare_media_cache(media_root))
        except Exception as exc:
            # Cache persistence is an optimization and must never turn a completed
            # production into a failure. Leave the Media namespace unsaved if its
            # sanitizer cannot prove it safe.
            print(f"Media durable cache envelope sanitization skipped ({type(exc).__name__})")
            media_valid = False

    return bool(valid) or media_valid


def _main() -> int:
    parser = argparse.ArgumentParser(description="Sanitize Isco durable stage cache before persistence")
    parser.add_argument("prepare", nargs="?")
    parser.add_argument("--root", required=True)
    args = parser.parse_args()
    allowed = prepare_cache_for_persistence(Path(args.root))
    print(f"save_allowed={'true' if allowed else 'false'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())

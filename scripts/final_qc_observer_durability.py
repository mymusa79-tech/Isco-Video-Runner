from __future__ import annotations

import hashlib
import importlib.metadata as metadata
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

from isco_video_agent.security import secret_free_subprocess_env

from scripts import final_master_qc
from scripts import groq_audio_audit
from scripts import voice_identity_observer
from scripts import voice_mesh
from scripts.runtime_reliability import production_entrypoint_modules


CACHE_SCHEMA_VERSION = 1
CACHE_NAMESPACE = "final-qc-observers-durable-v1"
MAX_FILE_BYTES = 1536 * 1024 * 1024
MAX_EVIDENCE_BYTES = 4 * 1024 * 1024
MAX_ENTRIES_BY_KIND = {
    "final-qc": 8,
    "groq-audio": 8,
    "voice": 128,
}
_FFMPEG_IDENTITY: dict[str, Any] | None = None


def _shared_root() -> Path | None:
    raw = (os.environ.get("ISCO_TTS_CACHE_PATH") or "").strip()
    return Path(raw) if raw else None


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _module_sha(module: Any) -> str:
    raw = getattr(module, "__file__", None)
    if not raw:
        return "unknown"
    try:
        return _sha256_file(Path(raw).resolve())
    except OSError:
        return "unknown"


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _binding_hash(binding: dict[str, Any]) -> str:
    return _sha256_bytes(_json_bytes(binding))


def _regular_file_binding(path: Path, *, max_bytes: int = MAX_FILE_BYTES) -> dict[str, Any] | None:
    path = Path(path)
    try:
        if path.is_symlink() or not path.is_file():
            return None
        size = path.stat().st_size
    except OSError:
        return None
    if size <= 0 or size > max_bytes:
        return None
    return {
        "sha256": _sha256_file(path),
        "byte_length": size,
        "suffix": path.suffix.lower(),
    }


def _contract_identity(*, require_run_id: bool = False) -> dict[str, str] | None:
    engine_sha = (os.environ.get("ISCO_ENGINE_SHA") or "").strip().lower()
    brief_sha = (os.environ.get("ISCO_APPROVED_BRIEF_SHA256") or "").strip().lower()
    run_id = (os.environ.get("GITHUB_RUN_ID") or "").strip()
    if len(engine_sha) != 40 or len(brief_sha) != 64:
        return None
    if require_run_id and not run_id:
        return None
    result = {
        "engine_sha": engine_sha,
        "approved_brief_sha256": brief_sha,
    }
    if require_run_id:
        result["github_run_id"] = run_id
    return result


def _binary_identity(name: str) -> dict[str, str] | None:
    try:
        output = subprocess.check_output(
            [name, "-version"],
            env=secret_free_subprocess_env(),
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:
        return None
    if not output:
        return None
    return {
        "first_line": output.splitlines()[0].strip(),
        "sha256": _sha256_bytes(output.encode("utf-8")),
    }


def _media_tools_identity() -> dict[str, Any] | None:
    global _FFMPEG_IDENTITY
    if _FFMPEG_IDENTITY is not None:
        return dict(_FFMPEG_IDENTITY)
    ffmpeg = _binary_identity("ffmpeg")
    ffprobe = _binary_identity("ffprobe")
    if ffmpeg is None or ffprobe is None:
        return None
    _FFMPEG_IDENTITY = {"ffmpeg": ffmpeg, "ffprobe": ffprobe}
    return dict(_FFMPEG_IDENTITY)


def _entry(root: Path, kind: str, fingerprint: str) -> Path:
    if kind == "final-qc":
        return Path(root) / "final-qc" / fingerprint
    return Path(root) / "observers" / kind / fingerprint


def _atomic_copy(source: Path, dest: Path) -> None:
    source = Path(source)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.unlink(missing_ok=True)
    try:
        shutil.copyfile(source, tmp)
        with tmp.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(tmp, dest)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _load_object(path: Path) -> dict[str, Any] | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _persist_document(
    root: Path,
    kind: str,
    fingerprint: str,
    binding: dict[str, Any],
    document_path: Path,
) -> None:
    document_path = Path(document_path)
    file_binding = _regular_file_binding(document_path, max_bytes=MAX_EVIDENCE_BYTES)
    if file_binding is None:
        return
    entry = _entry(root, kind, fingerprint)
    evidence = entry / "evidence.json"
    _atomic_copy(document_path, evidence)
    manifest = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "namespace": CACHE_NAMESPACE,
        "kind": kind,
        "fingerprint": fingerprint,
        "binding": binding,
        "evidence_sha256": _sha256_file(evidence),
        "evidence_bytes": evidence.stat().st_size,
    }
    _atomic_json(entry / "manifest.json", manifest)


def _validate_entry(
    root: Path,
    kind: str,
    fingerprint: str,
    binding: dict[str, Any] | None = None,
) -> tuple[Path, dict[str, Any]] | None:
    entry = _entry(root, kind, fingerprint)
    manifest = _load_object(entry / "manifest.json")
    evidence = entry / "evidence.json"
    if (
        manifest is None
        or entry.is_symlink()
        or not entry.is_dir()
        or evidence.is_symlink()
        or not evidence.is_file()
    ):
        return None
    stored_binding = manifest.get("binding")
    if (
        manifest.get("schema_version") != CACHE_SCHEMA_VERSION
        or manifest.get("namespace") != CACHE_NAMESPACE
        or manifest.get("kind") != kind
        or not isinstance(stored_binding, dict)
        or manifest.get("fingerprint") != _binding_hash(stored_binding)
        or manifest.get("fingerprint") != fingerprint
    ):
        return None
    if binding is not None and stored_binding != binding:
        return None
    size = evidence.stat().st_size
    if size <= 0 or size > MAX_EVIDENCE_BYTES or manifest.get("evidence_bytes") != size:
        return None
    if _sha256_file(evidence) != str(manifest.get("evidence_sha256") or ""):
        return None
    document = _load_object(evidence)
    if document is None:
        return None
    return evidence, document


def _evict(root: Path, kind: str, fingerprint: str) -> None:
    shutil.rmtree(_entry(root, kind, fingerprint), ignore_errors=True)


def _final_qc_binding(output_dir: Path) -> dict[str, Any] | None:
    root = Path(output_dir)
    contract = _contract_identity()
    tools = _media_tools_identity()
    inputs = {
        "final": _regular_file_binding(root / "final.mp4"),
        "plan": _regular_file_binding(root / "plan.json", max_bytes=MAX_EVIDENCE_BYTES),
        "quality": _regular_file_binding(root / "quality-final.json", max_bytes=MAX_EVIDENCE_BYTES),
        "timeline": _regular_file_binding(root / "visual-timeline.json", max_bytes=MAX_EVIDENCE_BYTES),
    }
    if contract is None or tools is None or any(value is None for value in inputs.values()):
        return None
    return {
        "namespace": CACHE_NAMESPACE,
        "schema_version": CACHE_SCHEMA_VERSION,
        "kind": "final-qc",
        **contract,
        "implementation_sha256": _module_sha(final_master_qc),
        "media_tools": tools,
        "inputs": inputs,
    }


def _passing_final_qc(document: dict[str, Any]) -> bool:
    return bool(
        document.get("schema_version") == final_master_qc.SCHEMA_VERSION
        and document.get("status") == "pass"
        and document.get("production_stage") == "post_render_pre_gold_acceptance"
        and document.get("full_decode_ok") is True
        and document.get("full_decode_timed_out") is False
        and document.get("final_media_mutated") is False
        and not list(document.get("blocking_findings") or [])
    )


def run_final_master_qc_durable(
    output_dir: Path,
    *,
    original: Callable[[Path], dict[str, Any]] = final_master_qc.run_final_master_qc,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    shared = _shared_root()
    binding = _final_qc_binding(output_dir) if shared is not None else None
    if shared is not None and binding is not None:
        fingerprint = _binding_hash(binding)
        restored = _validate_entry(shared, "final-qc", fingerprint, binding)
        if restored is not None:
            evidence, document = restored
            if _passing_final_qc(document):
                _atomic_copy(evidence, output_dir / "final-master-qc.json")
                print(f"Final Master QC durable HIT fingerprint={fingerprint[:12]}; exact PASS evidence restored")
                return document
            _evict(shared, "final-qc", fingerprint)

    result = original(output_dir)
    if shared is not None and binding is not None and _passing_final_qc(result):
        fingerprint = _binding_hash(binding)
        try:
            _persist_document(
                shared,
                "final-qc",
                fingerprint,
                binding,
                output_dir / "final-master-qc.json",
            )
            print(f"Final Master QC durable PASS stored fingerprint={fingerprint[:12]}")
        except Exception as exc:
            print(f"Final Master QC durable persistence skipped ({type(exc).__name__})")
    return result


def _groq_binding(output_dir: Path, *, model: str) -> dict[str, Any] | None:
    root = Path(output_dir)
    contract = _contract_identity(require_run_id=True)
    tools = _media_tools_identity()
    final = _regular_file_binding(root / "final.mp4")
    plan = _regular_file_binding(root / "plan.json", max_bytes=MAX_EVIDENCE_BYTES)
    if contract is None or tools is None or final is None or plan is None:
        return None
    return {
        "namespace": CACHE_NAMESPACE,
        "schema_version": CACHE_SCHEMA_VERSION,
        "kind": "groq-audio",
        **contract,
        "implementation_sha256": _module_sha(groq_audio_audit),
        "model": str(model),
        "media_tools": tools,
        "final": final,
        "plan": plan,
    }


def _successful_groq_document(document: dict[str, Any], *, model: str) -> bool:
    governor = document.get("groq_governor")
    transcription = document.get("transcription")
    return bool(
        document.get("schema_version") == 1
        and document.get("mode") == groq_audio_audit.MODE
        and document.get("decision") in {"pass", "review"}
        and isinstance(governor, dict)
        and governor.get("status") == "ok"
        and isinstance(transcription, dict)
        and transcription.get("model") == model
        and not document.get("audit_error")
    )


def run_groq_audio_audit_durable(
    output_dir: Path,
    *,
    api_key: str | None,
    model: str = groq_audio_audit.DEFAULT_AUDIO_MODEL,
    original: Callable[..., dict[str, Any]] = groq_audio_audit.run_groq_audio_audit,
    **kwargs: Any,
) -> dict[str, Any]:
    # Custom test/injection seams are deliberately not memoized. Production uses the
    # canonical transcriber/extractor only; opaque alternative callables are live-only.
    if kwargs:
        return original(output_dir, api_key=api_key, model=model, **kwargs)

    output_dir = Path(output_dir)
    shared = _shared_root()
    binding = _groq_binding(output_dir, model=model) if shared is not None else None
    if shared is not None and binding is not None:
        fingerprint = _binding_hash(binding)
        restored = _validate_entry(shared, "groq-audio", fingerprint, binding)
        if restored is not None:
            evidence, document = restored
            if _successful_groq_document(document, model=model):
                _atomic_copy(evidence, output_dir / groq_audio_audit.AUDIT_FILENAME)
                print(f"Groq Audio Observer durable HIT fingerprint={fingerprint[:12]}; provider call skipped")
                return document
            _evict(shared, "groq-audio", fingerprint)

    result = original(output_dir, api_key=api_key, model=model)
    if shared is not None and binding is not None and _successful_groq_document(result, model=model):
        fingerprint = _binding_hash(binding)
        try:
            _persist_document(
                shared,
                "groq-audio",
                fingerprint,
                binding,
                output_dir / groq_audio_audit.AUDIT_FILENAME,
            )
            print(f"Groq Audio Observer durable result stored fingerprint={fingerprint[:12]}")
        except Exception as exc:
            print(f"Groq Audio Observer durable persistence skipped ({type(exc).__name__})")
    return result


def _package_version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "missing"


def _voice_binding(
    *,
    task_id: str,
    transcript: str,
    output: Path,
    model: str,
    requested_voice: str,
) -> dict[str, Any] | None:
    output_binding = _regular_file_binding(Path(output))
    profile_binding = _regular_file_binding(
        voice_identity_observer.PROFILE_PATH,
        max_bytes=MAX_EVIDENCE_BYTES,
    )
    provenance = voice_mesh.peek_voice_provenance(Path(output))
    provider = str(provenance.get("provider") or "unknown")
    fallback = provenance.get("fallback_used")
    if output_binding is None or profile_binding is None or provider == "unknown" or not isinstance(fallback, bool):
        return None
    return {
        "namespace": CACHE_NAMESPACE,
        "schema_version": CACHE_SCHEMA_VERSION,
        "kind": "voice",
        "task_id": str(task_id),
        "transcript_sha256": _sha256_bytes(str(transcript).encode("utf-8")),
        "audio": output_binding,
        "model": str(model),
        "requested_voice": str(requested_voice),
        "dialogue_mode": bool(voice_identity_observer._dialogue_mode(str(transcript))),
        "provider": provider,
        "fallback_used": fallback,
        "observer_sha256": _module_sha(voice_identity_observer),
        "profile": profile_binding,
        "backend_runtime": {
            "speechbrain": _package_version("speechbrain"),
            "torch": _package_version("torch"),
            "torchaudio": _package_version("torchaudio"),
        },
    }


def _voice_entry_from_audit(output: Path, *, task_id: str) -> dict[str, Any] | None:
    path = voice_identity_observer._audit_path(Path(output))
    document = _load_object(path)
    sections = document.get("sections") if isinstance(document, dict) else None
    if not isinstance(sections, list):
        return None
    for item in reversed(sections):
        if isinstance(item, dict) and item.get("task_id") == task_id:
            return dict(item)
    return None


def _successful_voice_entry(entry: dict[str, Any], binding: dict[str, Any]) -> bool:
    return bool(
        entry.get("task_id") == binding.get("task_id")
        and entry.get("model") == binding.get("model")
        and entry.get("requested_voice") == binding.get("requested_voice")
        and entry.get("dialogue_mode") is binding.get("dialogue_mode")
        and entry.get("provider") == binding.get("provider")
        and entry.get("fallback_used") is binding.get("fallback_used")
        and entry.get("decision") != "audit_error"
    )


def _persist_voice_entry(
    shared: Path,
    fingerprint: str,
    binding: dict[str, Any],
    entry: dict[str, Any],
) -> None:
    entry_dir = _entry(shared, "voice", fingerprint)
    source = entry_dir / ".entry-source.json"
    try:
        _atomic_json(source, entry)
        _persist_document(shared, "voice", fingerprint, binding, source)
    finally:
        source.unlink(missing_ok=True)


def _install_voice_observer_durability() -> None:
    current = voice_identity_observer.observe_output
    if getattr(current, "_isco_voice_observer_durable", False) is True:
        return

    def wrapped(
        *,
        task_id: str,
        transcript: str,
        output: Path,
        model: str,
        requested_voice: str,
    ) -> None:
        shared = _shared_root()
        binding = (
            _voice_binding(
                task_id=task_id,
                transcript=transcript,
                output=Path(output),
                model=model,
                requested_voice=requested_voice,
            )
            if shared is not None
            else None
        )
        if shared is not None and binding is not None:
            fingerprint = _binding_hash(binding)
            restored = _validate_entry(shared, "voice", fingerprint, binding)
            if restored is not None:
                _evidence, entry = restored
                if _successful_voice_entry(entry, binding):
                    consumed = voice_mesh.consume_voice_provenance(Path(output))
                    if (
                        consumed.get("provider") == binding.get("provider")
                        and consumed.get("fallback_used") is binding.get("fallback_used")
                    ):
                        voice_identity_observer._append_entry(
                            voice_identity_observer._audit_path(Path(output)),
                            entry,
                        )
                        print(f"Voice Identity Observer durable HIT fingerprint={fingerprint[:12]}")
                        return
                    # Preserve the original observer's one-time provenance semantics if
                    # a concurrent/test mutation changed provenance between peek/consume.
                    if isinstance(consumed.get("fallback_used"), bool):
                        voice_mesh.record_voice_provenance(
                            Path(output),
                            provider=str(consumed.get("provider") or "unknown"),
                            fallback_used=bool(consumed["fallback_used"]),
                        )
                else:
                    _evict(shared, "voice", fingerprint)

        current(
            task_id=task_id,
            transcript=transcript,
            output=Path(output),
            model=model,
            requested_voice=requested_voice,
        )
        if shared is not None and binding is not None:
            entry = _voice_entry_from_audit(Path(output), task_id=task_id)
            if entry is not None and _successful_voice_entry(entry, binding):
                fingerprint = _binding_hash(binding)
                try:
                    _persist_voice_entry(shared, fingerprint, binding, entry)
                    print(f"Voice Identity Observer durable entry stored fingerprint={fingerprint[:12]}")
                except Exception as exc:
                    print(f"Voice Identity Observer durable persistence skipped ({type(exc).__name__})")

    wrapped._isco_voice_observer_durable = True
    wrapped._isco_voice_observer_durable_original = current
    voice_identity_observer.observe_output = wrapped


def _install_final_qc_durability() -> None:
    for production in production_entrypoint_modules():
        current = getattr(production, "run_final_master_qc", None)
        if not callable(current) or getattr(current, "_isco_final_qc_durable", False) is True:
            continue

        def make_wrapper(original: Callable[[Path], dict[str, Any]]):
            def wrapped(output_dir: Path) -> dict[str, Any]:
                return run_final_master_qc_durable(Path(output_dir), original=original)

            wrapped._isco_final_qc_durable = True
            wrapped._isco_final_qc_durable_original = original
            return wrapped

        setattr(production, "run_final_master_qc", make_wrapper(current))


def install_final_qc_observer_durability() -> None:
    """Install optimization-only durability around exact deterministic evidence.

    Final Master QC itself remains unchanged and authoritative. Only strict PASS evidence
    can be restored, bound to exact final/plan/quality/timeline bytes, exact QC code,
    pinned Engine and ffmpeg/ffprobe identities. Groq is same-run-id only because the
    provider model is opaque and can drift behind a stable model name. Voice Identity
    is local/pinned and may reuse exact per-section evidence. Analytics is deliberately
    excluded because it is time-varying and should refresh on every retry.
    """
    if _shared_root() is None:
        print("Final QC/Observer durable cache disabled: shared durable stage cache not configured")
        return
    _install_final_qc_durability()
    _install_voice_observer_durability()
    print("Final QC/Observer durability installed: Final QC PASS + Groq retry + Voice Identity; analytics stays live")


def _remove_path(path: Path) -> None:
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    except OSError:
        pass


def prepare_cache_for_persistence(shared_root: Path) -> bool:
    root = Path(shared_root)
    targets = {
        "final-qc": root / "final-qc",
        "groq-audio": root / "observers" / "groq-audio",
        "voice": root / "observers" / "voice",
    }
    valid_any = False
    for kind, kind_root in targets.items():
        if not kind_root.exists():
            continue
        if kind_root.is_symlink() or not kind_root.is_dir():
            _remove_path(kind_root)
            continue
        valid: list[tuple[float, Path]] = []
        for item in list(kind_root.iterdir()):
            if item.is_symlink() or not item.is_dir():
                _remove_path(item)
                continue
            manifest = _load_object(item / "manifest.json")
            fingerprint = str(manifest.get("fingerprint") or "") if manifest else ""
            checked = _validate_entry(root, kind, fingerprint) if fingerprint else None
            if checked is None:
                _remove_path(item)
                continue
            valid.append((item.stat().st_mtime, item))
        cap = MAX_ENTRIES_BY_KIND[kind]
        if len(valid) > cap:
            for _mtime, item in sorted(valid)[: len(valid) - cap]:
                _remove_path(item)
            valid = sorted(valid)[len(valid) - cap :]
        valid_any = valid_any or bool(valid)
    print(f"Final QC/Observer durable cache sanitized: save_allowed={valid_any}")
    return valid_any

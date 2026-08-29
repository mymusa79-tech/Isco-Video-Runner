from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import isco_video_agent.opening_director as opening_director
import isco_video_agent.orchestrator as orchestrator
import isco_video_agent.section_visual_sequence as section_visual_sequence
import isco_video_agent.visual_selection as visual_selection

from scripts import media_trust_boundary_v2 as trust
from scripts import security_v1_live_binding as security_v1

CACHE_SCHEMA_VERSION = 1
CACHE_NAMESPACE = "media-shot-v1"
MIN_MEDIA_BYTES = 1024
MAX_RAW_BYTES = trust.MAX_DOWNLOAD_BYTES
MAX_PREPARED_BYTES = 512 * 1024 * 1024
MAX_RAW_ENTRIES = 32
MAX_AUDIT_ENTRIES = 64
MAX_PREPARED_ENTRIES = 32
MAX_TOTAL_BYTES = 1536 * 1024 * 1024

_INSTALLED = False
_ORIGINAL_TRUSTED_DOWNLOAD: Callable[..., Path] | None = None
_ORIGINAL_PREPARE_CLIP: Callable[..., Path] | None = None
_ORIGINAL_REVIEW_FUNCTIONS: dict[str, Callable[..., Any]] = {}
_LOCAL_REVALIDATED_RAW: set[str] = set()

_TRANSIENT_AUDIT_KEYS = {
    "candidate_inspection_index",
    "is_selected",
    "opening_slot",
    "is_final_cut_auxiliary",
    "is_section_sequence_member",
    "section_sequence_slot",
}


def _cache_root() -> Path | None:
    explicit = (os.environ.get("ISCO_MEDIA_CACHE_PATH") or "").strip()
    if explicit:
        return Path(explicit)
    # Media owns its own semantic namespace but reuses the already-hardened durable
    # stage transport. The TTS workflow restores/saves this parent even after failure;
    # nesting Media here gives cross-run persistence without a second competing Actions
    # cache owner or duplicate save race.
    shared = (os.environ.get("ISCO_TTS_CACHE_PATH") or "").strip()
    return Path(shared) / "media" if shared else None


def _required_env(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise RuntimeError(f"media_cache_missing_contract_env:{name}")
    return value


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _module_sha(module) -> str:
    return _sha256_file(Path(module.__file__).resolve())


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _candidate_sha(candidate: dict) -> str | None:
    try:
        return _sha256_bytes(_json_bytes(candidate))
    except (TypeError, ValueError):
        return None


def _binding_hash(binding: dict[str, Any]) -> str:
    return _sha256_bytes(_json_bytes(binding))


def _contract_hash(kind: str, *parts: str) -> str:
    payload = {"kind": kind, "namespace": CACHE_NAMESPACE, "schema": CACHE_SCHEMA_VERSION, "parts": list(parts)}
    return _binding_hash(payload)


def _raw_contract() -> str:
    return _contract_hash("raw", _module_sha(trust), _module_sha(__import__(__name__, fromlist=["*"])))


def _audit_contract() -> str:
    return _contract_hash(
        "audit",
        _raw_contract(),
        _module_sha(security_v1),
        _required_env("ISCO_ENGINE_SHA"),
        _required_env("GEMINI_CONTENT_MODEL"),
    )


def _prepared_contract() -> str:
    return _contract_hash(
        "prepared",
        _raw_contract(),
        _required_env("ISCO_ENGINE_SHA"),
    )


def _raw_key(provider: str, source_url: str) -> str:
    return _sha256_bytes((provider.strip().casefold() + "\x1f" + source_url.strip()).encode("utf-8"))


def _safe_suffix(value: str) -> str:
    suffix = Path(value).suffix.lower()
    if not suffix or len(suffix) > 10 or not suffix.startswith(".") or not suffix[1:].isalnum():
        return ".bin"
    return suffix


def _raw_entry(root: Path, raw_key: str) -> Path:
    return root / "raw" / raw_key


def _audit_path(root: Path, fingerprint: str) -> Path:
    return root / "audits" / f"{fingerprint}.json"


def _prepared_entry(root: Path, fingerprint: str) -> Path:
    return root / "prepared" / fingerprint


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


def _atomic_copy(source: Path, dest: Path) -> None:
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


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _candidate_urls(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for item in value.values():
            found.update(_candidate_urls(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_candidate_urls(item))
    elif isinstance(value, str) and value.startswith("https://"):
        found.add(value)
    return found


def _trusted_record_for_candidate(provider: str, candidate: dict):
    provider = provider.strip().casefold()
    matches = []
    for url in _candidate_urls(candidate):
        record = trust._records_by_url.get((provider, url))
        if record is not None:
            matches.append(record)
    unique = {(record.source_url, record.sha256): record for record in matches}
    if len(unique) != 1:
        return None
    return next(iter(unique.values()))


def _persist_raw(root: Path, record, materialized_path: Path) -> str | None:
    try:
        provider = str(record.provider).strip().casefold()
        source_url = str(record.source_url).strip()
        final_url = str(record.final_url).strip()
        trust._validate_provider_url(provider, source_url)
        trust._validate_provider_url(provider, final_url)
        source = Path(record.quarantine_path)
        if source.is_symlink() or not source.is_file():
            return None
        size = source.stat().st_size
        if size <= 0 or size > MAX_RAW_BYTES or size != int(record.byte_length):
            return None
        sha = _sha256_file(source)
        if sha != record.sha256:
            return None
        raw_key = _raw_key(provider, source_url)
        entry = _raw_entry(root, raw_key)
        suffix = _safe_suffix(str(materialized_path))
        cached = entry / f"source{suffix}"
        _atomic_copy(source, cached)
        manifest = {
            "schema_version": CACHE_SCHEMA_VERSION,
            "kind": "raw",
            "raw_key": raw_key,
            "contract_sha256": _raw_contract(),
            "provider": provider,
            "source_url": source_url,
            "final_url": final_url,
            "sha256": sha,
            "byte_length": size,
            "filename": cached.name,
        }
        _atomic_json(entry / "manifest.json", manifest)
        return raw_key
    except Exception as exc:
        print(f"Media durable raw persistence skipped ({type(exc).__name__})")
        return None


def _validate_raw(root: Path, raw_key: str, *, provider: str | None = None, source_url: str | None = None, sha256: str | None = None):
    entry = _raw_entry(root, raw_key)
    manifest = _load_json(entry / "manifest.json")
    if manifest is None or entry.is_symlink() or not entry.is_dir():
        return None
    if manifest.get("schema_version") != CACHE_SCHEMA_VERSION or manifest.get("kind") != "raw":
        return None
    if manifest.get("raw_key") != raw_key or manifest.get("contract_sha256") != _raw_contract():
        return None
    stored_provider = str(manifest.get("provider") or "").strip().casefold()
    stored_url = str(manifest.get("source_url") or "").strip()
    final_url = str(manifest.get("final_url") or "").strip()
    if provider is not None and stored_provider != provider.strip().casefold():
        return None
    if source_url is not None and stored_url != source_url.strip():
        return None
    if _raw_key(stored_provider, stored_url) != raw_key:
        return None
    try:
        trust._validate_provider_url(stored_provider, stored_url)
        trust._validate_provider_url(stored_provider, final_url)
    except Exception:
        return None
    filename = str(manifest.get("filename") or "")
    if not filename or Path(filename).name != filename:
        return None
    media = entry / filename
    if media.is_symlink() or not media.is_file():
        return None
    try:
        size = media.stat().st_size
    except OSError:
        return None
    if size <= 0 or size > MAX_RAW_BYTES or manifest.get("byte_length") != size:
        return None
    stored_sha = manifest.get("sha256")
    if not isinstance(stored_sha, str) or _sha256_file(media) != stored_sha:
        return None
    if sha256 is not None and stored_sha != sha256:
        return None
    return media, manifest


def _revalidate_cached_raw(raw_key: str, media: Path) -> bool:
    """Re-run current deterministic Trust/Security gates on restored exact bytes once."""
    if raw_key in _LOCAL_REVALIDATED_RAW:
        return True
    try:
        local_block = trust._inspect_exact_review_source(Path(media))
        if local_block is not None:
            return False
        trust._distributed_scan_media_before_vision(Path(media))
    except Exception:
        return False
    _LOCAL_REVALIDATED_RAW.add(raw_key)
    return True


def _restore_raw(root: Path, provider: str, source_url: str, dest: Path) -> Path | None:
    provider = provider.strip().casefold()
    source_url = source_url.strip()
    raw_key = _raw_key(provider, source_url)
    valid = _validate_raw(root, raw_key, provider=provider, source_url=source_url)
    if valid is None:
        return None
    media, manifest = valid
    # A digest/cache-contract match proves identity, not current safety. Re-run the
    # live deterministic stock preflight and distributed Security V1 scan before a
    # cached blob may regain TrustedMediaRecord authority in this process.
    if not _revalidate_cached_raw(raw_key, media):
        print(f"Media durable raw restore rejected (local_trust_security) key={raw_key[:12]}")
        return None
    suffix = _safe_suffix(media.name)
    quarantine = trust._root() / f"durable-{raw_key}{suffix}"
    try:
        _atomic_copy(media, quarantine)
        record = trust.TrustedMediaRecord(
            provider=provider,
            source_url=source_url,
            final_url=str(manifest["final_url"]),
            sha256=str(manifest["sha256"]),
            byte_length=int(manifest["byte_length"]),
            quarantine_path=quarantine,
        )
        trust._records_by_url[(provider, source_url)] = record
        result = trust._materialize_verified(record, Path(dest))
        print(f"Media durable raw HIT provider={provider} key={raw_key[:12]}")
        return result
    except Exception as exc:
        quarantine.unlink(missing_ok=True)
        print(f"Media durable raw restore rejected ({type(exc).__name__})")
        return None


def _wrap_trusted_download(root: Path, original: Callable[..., Path]) -> Callable[..., Path]:
    def wrapped(provider: str, url: str, dest: Path) -> Path:
        provider_norm = str(provider).strip().casefold()
        source_url = str(url).strip()
        existing = trust._records_by_url.get((provider_norm, source_url))
        if existing is None:
            restored = _restore_raw(root, provider_norm, source_url, Path(dest))
            if restored is not None:
                return restored
        result = original(provider_norm, source_url, Path(dest))
        record = trust.trusted_record(result)
        if record is not None:
            _persist_raw(root, record, Path(result))
        return result

    wrapped._isco_media_durable_raw = True
    wrapped._isco_media_durable_original = original
    return wrapped


def _audit_binding(provider: str, asset_id: object, ctx_hash: str, candidate_sha256: str) -> dict[str, Any]:
    return {
        "namespace": CACHE_NAMESPACE,
        "schema_version": CACHE_SCHEMA_VERSION,
        "approved_brief_sha256": _required_env("ISCO_APPROVED_BRIEF_SHA256"),
        "engine_sha": _required_env("ISCO_ENGINE_SHA"),
        "vision_model": _required_env("GEMINI_CONTENT_MODEL"),
        "audit_contract_sha256": _audit_contract(),
        "provider": str(provider).strip().casefold(),
        "asset_id": str(asset_id),
        "context_hash": ctx_hash,
        "candidate_sha256": candidate_sha256,
    }


def _sanitize_cloud_audit(audit: dict) -> dict | None:
    if audit.get("vision_review_performed") is not True or audit.get("review_origin") != "cloud_visual_qa":
        return None
    if audit.get("status") not in {"pass", "block"}:
        return None
    return {key: value for key, value in audit.items() if key not in _TRANSIENT_AUDIT_KEYS}


def _persist_audit(root: Path, provider: str, candidate: dict, ctx_hash: str, audit: dict) -> None:
    candidate_sha256 = _candidate_sha(candidate)
    cleaned = _sanitize_cloud_audit(audit)
    record = _trusted_record_for_candidate(provider, candidate)
    if candidate_sha256 is None or cleaned is None or record is None:
        return
    raw_key = _persist_raw(root, record, Path(record.quarantine_path))
    if raw_key is None:
        return
    binding = _audit_binding(provider, candidate.get("id"), ctx_hash, candidate_sha256)
    fingerprint = _binding_hash(binding)
    document = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "kind": "audit",
        "fingerprint": fingerprint,
        "binding": binding,
        "raw_key": raw_key,
        "source_url": record.source_url,
        "source_sha256": record.sha256,
        "audit": cleaned,
        "audit_sha256": _sha256_bytes(_json_bytes(cleaned)),
    }
    _atomic_json(_audit_path(root, fingerprint), document)


def _load_persistent_audit(root: Path, provider: str, candidate: dict, narration_context: str, intended_visual: str) -> tuple[str, dict] | None:
    candidate_sha256 = _candidate_sha(candidate)
    if candidate_sha256 is None:
        return None
    ctx_hash = visual_selection.context_hash(narration_context, intended_visual)
    binding = _audit_binding(provider, candidate.get("id"), ctx_hash, candidate_sha256)
    fingerprint = _binding_hash(binding)
    document = _load_json(_audit_path(root, fingerprint))
    if document is None:
        return None
    if document.get("schema_version") != CACHE_SCHEMA_VERSION or document.get("kind") != "audit":
        return None
    if document.get("fingerprint") != fingerprint or document.get("binding") != binding:
        return None
    source_url = str(document.get("source_url") or "")
    if source_url not in _candidate_urls(candidate):
        return None
    raw_key = str(document.get("raw_key") or "")
    source_sha = str(document.get("source_sha256") or "")
    valid_raw = _validate_raw(root, raw_key, provider=provider, source_url=source_url, sha256=source_sha)
    if valid_raw is None:
        return None
    raw_media, _ = valid_raw
    audit = document.get("audit")
    if not isinstance(audit, dict) or _sanitize_cloud_audit(audit) is None:
        return None
    if document.get("audit_sha256") != _sha256_bytes(_json_bytes(audit)):
        return None

    # Durable semantic Vision reuse never bypasses the current local trust/security
    # rules. Both the stock-media structural preflight and distributed Security V1
    # frame/OCR scan are required before the old cloud verdict regains authority.
    if not _revalidate_cached_raw(raw_key, raw_media):
        return None
    return ctx_hash, dict(audit)


def _make_review_wrapper(root: Path, original: Callable[..., Any]) -> Callable[..., Any]:
    def wrapped(
        interleaved,
        *,
        narration_context: str,
        intended_visual: str,
        audit_fn,
        cache,
        max_candidates: int = 2,
        max_total_candidates: int | None = None,
    ):
        for provider, candidate in interleaved:
            asset_id = candidate.get("id")
            ctx_hash = visual_selection.context_hash(narration_context, intended_visual)
            if cache.get(provider, asset_id, ctx_hash) is not None:
                continue
            persistent = _load_persistent_audit(
                root,
                provider,
                candidate,
                narration_context,
                intended_visual,
            )
            if persistent is not None:
                persistent_ctx, audit = persistent
                cache.set(provider, asset_id, persistent_ctx, audit)

        result = original(
            interleaved,
            narration_context=narration_context,
            intended_visual=intended_visual,
            audit_fn=audit_fn,
            cache=cache,
            max_candidates=max_candidates,
            max_total_candidates=max_total_candidates,
        )
        ctx_hash = visual_selection.context_hash(narration_context, intended_visual)
        for review in result.reviewed:
            if not review.from_cache:
                try:
                    _persist_audit(root, review.provider, review.candidate, ctx_hash, review.audit)
                except Exception as exc:
                    print(f"Media durable audit persistence skipped ({type(exc).__name__})")
        return result

    wrapped._isco_media_durable_review = True
    wrapped._isco_media_durable_original = original
    return wrapped


def _prepared_binding(record, *, seconds: float, portrait: bool, fps: int) -> dict[str, Any]:
    return {
        "namespace": CACHE_NAMESPACE,
        "schema_version": CACHE_SCHEMA_VERSION,
        "approved_brief_sha256": _required_env("ISCO_APPROVED_BRIEF_SHA256"),
        "engine_sha": _required_env("ISCO_ENGINE_SHA"),
        "prepared_contract_sha256": _prepared_contract(),
        "provider": record.provider,
        "source_url": record.source_url,
        "source_sha256": record.sha256,
        "seconds_millis": int(round(float(seconds) * 1000.0)),
        "portrait": bool(portrait),
        "fps": int(fps),
    }


def _validate_prepared(root: Path, fingerprint: str, binding: dict[str, Any]):
    entry = _prepared_entry(root, fingerprint)
    manifest = _load_json(entry / "manifest.json")
    clip = entry / "clip.mp4"
    if manifest is None or entry.is_symlink() or clip.is_symlink() or not clip.is_file():
        return None
    if manifest.get("schema_version") != CACHE_SCHEMA_VERSION or manifest.get("kind") != "prepared":
        return None
    if manifest.get("fingerprint") != fingerprint or manifest.get("binding") != binding:
        return None
    raw_key = str(manifest.get("raw_key") or "")
    if _validate_raw(
        root,
        raw_key,
        provider=str(binding["provider"]),
        source_url=str(binding["source_url"]),
        sha256=str(binding["source_sha256"]),
    ) is None:
        return None
    size = clip.stat().st_size
    if size < MIN_MEDIA_BYTES or size > MAX_PREPARED_BYTES or manifest.get("byte_length") != size:
        return None
    sha = manifest.get("sha256")
    if not isinstance(sha, str) or _sha256_file(clip) != sha:
        return None
    return clip, manifest


def _wrap_prepare_clip(root: Path, original: Callable[..., Path]) -> Callable[..., Path]:
    def wrapped(src: Path, dest: Path, seconds: float, portrait: bool, fps: int = 30) -> Path:
        src = Path(src)
        dest = Path(dest)
        record = trust.trusted_record(src)
        if record is None:
            return original(src, dest, seconds, portrait, fps=fps)
        binding = _prepared_binding(record, seconds=seconds, portrait=portrait, fps=fps)
        fingerprint = _binding_hash(binding)
        cached = _validate_prepared(root, fingerprint, binding)
        if cached is not None:
            clip, _manifest = cached
            tmp = dest.with_name(dest.name + ".media-cache.tmp")
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copyfile(clip, tmp)
                os.replace(tmp, dest)
                rendered_seconds = float(orchestrator.duration(dest))
                target = float(seconds)
                if rendered_seconds + 0.25 < target or rendered_seconds > target + 1.0:
                    raise RuntimeError("media_cache_prepared_duration_mismatch")
                print(f"Media durable prepared HIT fingerprint={fingerprint[:12]}")
                return dest
            except Exception as exc:
                tmp.unlink(missing_ok=True)
                dest.unlink(missing_ok=True)
                print(f"Media durable prepared restore rejected ({type(exc).__name__}); rerendering")

        result = Path(original(src, dest, seconds, portrait, fps=fps))
        try:
            if result.is_symlink() or not result.is_file():
                return result
            size = result.stat().st_size
            if size < MIN_MEDIA_BYTES or size > MAX_PREPARED_BYTES:
                return result
            raw_key = _persist_raw(root, record, src)
            if raw_key is None:
                return result
            entry = _prepared_entry(root, fingerprint)
            clip = entry / "clip.mp4"
            _atomic_copy(result, clip)
            manifest = {
                "schema_version": CACHE_SCHEMA_VERSION,
                "kind": "prepared",
                "fingerprint": fingerprint,
                "binding": binding,
                "raw_key": raw_key,
                "sha256": _sha256_file(clip),
                "byte_length": clip.stat().st_size,
            }
            _atomic_json(entry / "manifest.json", manifest)
        except Exception as exc:
            print(f"Media durable prepared persistence skipped ({type(exc).__name__})")
        return result

    wrapped._isco_media_durable_prepare = True
    wrapped._isco_media_durable_original = original
    return wrapped


def install_media_durable_cache() -> None:
    """Install cross-run stock decision/raw/prepared reuse without changing safety budgets."""
    global _INSTALLED, _ORIGINAL_TRUSTED_DOWNLOAD, _ORIGINAL_PREPARE_CLIP
    root = _cache_root()
    if root is None:
        print("Media durable cache disabled: durable stage cache not configured")
        return
    if _INSTALLED:
        return
    root.mkdir(parents=True, exist_ok=True)

    _ORIGINAL_TRUSTED_DOWNLOAD = trust.trusted_download
    trust.trusted_download = _wrap_trusted_download(root, _ORIGINAL_TRUSTED_DOWNLOAD)

    review_wrapper = _make_review_wrapper(root, visual_selection.review_candidates)
    _ORIGINAL_REVIEW_FUNCTIONS["visual_selection"] = visual_selection.review_candidates
    _ORIGINAL_REVIEW_FUNCTIONS["opening_director"] = opening_director.review_candidates
    _ORIGINAL_REVIEW_FUNCTIONS["section_visual_sequence"] = section_visual_sequence.review_candidates
    visual_selection.review_candidates = review_wrapper
    opening_director.review_candidates = review_wrapper
    section_visual_sequence.review_candidates = review_wrapper

    _ORIGINAL_PREPARE_CLIP = orchestrator.prepare_clip
    orchestrator.prepare_clip = _wrap_prepare_clip(root, _ORIGINAL_PREPARE_CLIP)
    _INSTALLED = True
    print(
        "Media durable cache installed: exact candidate/context Vision reuse with full local revalidation, "
        "trusted raw reuse, prepared-clip reuse; no search/Vision/retry budget expansion"
    )


def reset_media_durable_cache_for_tests() -> None:
    global _INSTALLED, _ORIGINAL_TRUSTED_DOWNLOAD, _ORIGINAL_PREPARE_CLIP
    # Tests may install the durable layer while unittest.mock owns an outer patch. If
    # that outer context has already restored its target, never resurrect the stale
    # test-local callable captured during install. Only unwind wrappers we still own.
    if _ORIGINAL_TRUSTED_DOWNLOAD is not None and getattr(
        trust.trusted_download, "_isco_media_durable_raw", False
    ):
        trust.trusted_download = _ORIGINAL_TRUSTED_DOWNLOAD
    if _ORIGINAL_PREPARE_CLIP is not None and getattr(
        orchestrator.prepare_clip, "_isco_media_durable_prepare", False
    ):
        orchestrator.prepare_clip = _ORIGINAL_PREPARE_CLIP
    if "visual_selection" in _ORIGINAL_REVIEW_FUNCTIONS and getattr(
        visual_selection.review_candidates, "_isco_media_durable_review", False
    ):
        visual_selection.review_candidates = _ORIGINAL_REVIEW_FUNCTIONS["visual_selection"]
    if "opening_director" in _ORIGINAL_REVIEW_FUNCTIONS and getattr(
        opening_director.review_candidates, "_isco_media_durable_review", False
    ):
        opening_director.review_candidates = _ORIGINAL_REVIEW_FUNCTIONS["opening_director"]
    if "section_visual_sequence" in _ORIGINAL_REVIEW_FUNCTIONS and getattr(
        section_visual_sequence.review_candidates, "_isco_media_durable_review", False
    ):
        section_visual_sequence.review_candidates = _ORIGINAL_REVIEW_FUNCTIONS["section_visual_sequence"]
    _ORIGINAL_REVIEW_FUNCTIONS.clear()
    _LOCAL_REVALIDATED_RAW.clear()
    _ORIGINAL_TRUSTED_DOWNLOAD = None
    _ORIGINAL_PREPARE_CLIP = None
    _INSTALLED = False


def _remove_path(path: Path) -> None:
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    except OSError:
        pass


def prepare_cache_for_persistence(root: Path) -> bool:
    """Fail-soft sanitizer: keep only validated raw bytes referenced by valid cloud audits/prepared clips."""
    root = Path(root)
    audits_root = root / "audits"
    prepared_root = root / "prepared"
    raw_root = root / "raw"
    referenced_raw: set[str] = set()
    audit_docs: list[tuple[float, Path, dict[str, Any]]] = []
    prepared_docs: list[tuple[float, Path, dict[str, Any]]] = []

    if audits_root.is_dir() and not audits_root.is_symlink():
        for path in list(audits_root.iterdir()):
            doc = _load_json(path)
            if doc is None or doc.get("kind") != "audit" or doc.get("schema_version") != CACHE_SCHEMA_VERSION:
                _remove_path(path)
                continue
            binding = doc.get("binding")
            if not isinstance(binding, dict) or doc.get("fingerprint") != _binding_hash(binding):
                _remove_path(path)
                continue
            audit = doc.get("audit")
            if not isinstance(audit, dict) or _sanitize_cloud_audit(audit) is None:
                _remove_path(path)
                continue
            if doc.get("audit_sha256") != _sha256_bytes(_json_bytes(audit)):
                _remove_path(path)
                continue
            raw_key = str(doc.get("raw_key") or "")
            if _validate_raw(root, raw_key, provider=str(binding.get("provider") or ""), source_url=str(doc.get("source_url") or ""), sha256=str(doc.get("source_sha256") or "")) is None:
                _remove_path(path)
                continue
            referenced_raw.add(raw_key)
            audit_docs.append((path.stat().st_mtime, path, doc))

    if len(audit_docs) > MAX_AUDIT_ENTRIES:
        for _, path, doc in sorted(audit_docs)[: len(audit_docs) - MAX_AUDIT_ENTRIES]:
            _remove_path(path)
        audit_docs = sorted(audit_docs)[-MAX_AUDIT_ENTRIES:]
        referenced_raw = {str(doc.get("raw_key") or "") for _, _, doc in audit_docs}

    if prepared_root.is_dir() and not prepared_root.is_symlink():
        for entry in list(prepared_root.iterdir()):
            manifest = _load_json(entry / "manifest.json") if entry.is_dir() and not entry.is_symlink() else None
            if manifest is None or manifest.get("kind") != "prepared" or manifest.get("schema_version") != CACHE_SCHEMA_VERSION:
                _remove_path(entry)
                continue
            binding = manifest.get("binding")
            fingerprint = str(manifest.get("fingerprint") or "")
            if not isinstance(binding, dict) or fingerprint != _binding_hash(binding):
                _remove_path(entry)
                continue
            if _validate_prepared(root, fingerprint, binding) is None:
                _remove_path(entry)
                continue
            raw_key = str(manifest.get("raw_key") or "")
            referenced_raw.add(raw_key)
            prepared_docs.append((entry.stat().st_mtime, entry, manifest))

    if len(prepared_docs) > MAX_PREPARED_ENTRIES:
        for _, entry, _manifest in sorted(prepared_docs)[: len(prepared_docs) - MAX_PREPARED_ENTRIES]:
            _remove_path(entry)
        prepared_docs = sorted(prepared_docs)[-MAX_PREPARED_ENTRIES:]

    # Recompute raw references after entry caps.
    referenced_raw = {
        str(doc.get("raw_key") or "") for _, _, doc in audit_docs if Path(_).exists()
    } if False else set()
    for _mtime, path, doc in audit_docs:
        if path.exists():
            referenced_raw.add(str(doc.get("raw_key") or ""))
    for _mtime, entry, manifest in prepared_docs:
        if entry.exists():
            referenced_raw.add(str(manifest.get("raw_key") or ""))

    raw_entries: list[tuple[float, Path, int, str]] = []
    if raw_root.is_dir() and not raw_root.is_symlink():
        for entry in list(raw_root.iterdir()):
            raw_key = entry.name
            valid = _validate_raw(root, raw_key)
            if valid is None or raw_key not in referenced_raw:
                _remove_path(entry)
                continue
            media, _manifest = valid
            raw_entries.append((entry.stat().st_mtime, entry, media.stat().st_size, raw_key))

    def evict_raw(raw_key: str, entry: Path) -> None:
        _remove_path(entry)
        for _mtime, path, doc in audit_docs:
            if str(doc.get("raw_key") or "") == raw_key:
                _remove_path(path)
        for _mtime, prepared_entry, manifest in prepared_docs:
            if str(manifest.get("raw_key") or "") == raw_key:
                _remove_path(prepared_entry)

    if len(raw_entries) > MAX_RAW_ENTRIES:
        excess = len(raw_entries) - MAX_RAW_ENTRIES
        for _mtime, entry, _size, raw_key in sorted(raw_entries)[:excess]:
            evict_raw(raw_key, entry)
        raw_entries = sorted(raw_entries)[excess:]

    total = sum(size for _mtime, _entry, size, _key in raw_entries)
    for _mtime, entry, _size, _key in prepared_docs:
        clip = entry / "clip.mp4"
        if clip.is_file():
            total += clip.stat().st_size
    if total > MAX_TOTAL_BYTES:
        for _mtime, entry, size, raw_key in sorted(raw_entries):
            if total <= MAX_TOTAL_BYTES:
                break
            related_prepared = [p for _m, p, doc in prepared_docs if str(doc.get("raw_key") or "") == raw_key and p.exists()]
            related_size = sum((p / "clip.mp4").stat().st_size for p in related_prepared if (p / "clip.mp4").is_file())
            evict_raw(raw_key, entry)
            total -= size + related_size

    remaining = False
    for folder in (audits_root, prepared_root):
        if folder.is_dir() and any(folder.iterdir()):
            remaining = True
            break
    print(f"Media durable cache sanitized: save_allowed={remaining}")
    return remaining


def _main() -> int:
    parser = argparse.ArgumentParser(description="Sanitize Isco durable Media cache")
    parser.add_argument("prepare", nargs="?")
    parser.add_argument("--root", required=True)
    args = parser.parse_args()
    allowed = prepare_cache_for_persistence(Path(args.root))
    print(f"save_allowed={'true' if allowed else 'false'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())

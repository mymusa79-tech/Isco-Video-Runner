from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Callable

import isco_video_agent.orchestrator as orchestrator

from scripts import media_durable_asset_cache as asset_cache
from scripts import media_trust_boundary_v2 as media_trust
from scripts.media_durable_cache_auth import sign_document, verify_document


SCHEMA_VERSION = 1
NAMESPACE = "media-selection-v1"
SELECTION_DIRNAME = "selection-v1"
SELECTION_FILENAME = "selection.json"
MAX_SELECTION_RECORDS = 64
MAX_SELECTION_RECORD_BYTES = 1024 * 1024

_original_single: Callable[..., Any] | None = None
_original_opening: Callable[..., Any] | None = None
_original_section: Callable[..., Any] | None = None


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _module_sha256(module: Any) -> str:
    return _sha256_file(Path(module.__file__).resolve())


def _selection_contract_sha256() -> str:
    visual_selection = __import__("isco_video_agent.visual_selection", fromlist=["*"])
    opening_director = __import__("isco_video_agent.opening_director", fromlist=["*"])
    section_sequence = __import__("isco_video_agent.section_visual_sequence", fromlist=["*"])
    gemini_provider = __import__("isco_video_agent.providers.gemini", fromlist=["*"])
    stock_preflight = __import__("isco_video_agent.stock_media_preflight", fromlist=["*"])
    runner_security = __import__("scripts.security_v1", fromlist=["*"])
    payload = {
        "engine_orchestrator": _module_sha256(orchestrator),
        "engine_visual_selection": _module_sha256(visual_selection),
        "engine_opening_director": _module_sha256(opening_director),
        "engine_section_visual_sequence": _module_sha256(section_sequence),
        "engine_gemini_visual_audit": _module_sha256(gemini_provider),
        "engine_stock_media_preflight": _module_sha256(stock_preflight),
        "runner_media_trust": _module_sha256(media_trust),
        "runner_security_v1": _module_sha256(runner_security),
        "runner_selection_resume": _sha256_file(Path(__file__).resolve()),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _sha256_bytes(canonical)


def _text_sha(value: str) -> str:
    return _sha256_bytes(str(value).strip().encode("utf-8"))


def _semantic_contract(
    *,
    kind: str,
    narration_context: str,
    intended_visual: str,
    portrait: bool,
    seconds: float,
) -> dict[str, Any]:
    if kind not in {"single", "opening", "section_sequence"}:
        raise ValueError("unsupported media selection kind")
    return {
        "schema_version": SCHEMA_VERSION,
        "namespace": NAMESPACE,
        "kind": kind,
        "narration_sha256": _text_sha(narration_context),
        "intended_visual_sha256": _text_sha(intended_visual),
        "portrait": bool(portrait),
        "seconds": f"{float(seconds):.6f}",
        "content_model": str(os.environ.get("GEMINI_CONTENT_MODEL") or "").strip(),
        "engine_sha": str(os.environ.get("ISCO_ENGINE_SHA") or "").strip(),
        "selection_contract_sha256": _selection_contract_sha256(),
    }


def _selection_fingerprint(semantic: dict[str, Any]) -> str:
    canonical = json.dumps(
        semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return _sha256_bytes(canonical)


def _selection_root(root: Path) -> Path:
    path = root / SELECTION_DIRNAME
    if path.exists() and path.is_symlink():
        raise RuntimeError("media_selection_root_symlink_rejected")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _entry_dir(root: Path, fingerprint: str) -> Path:
    if len(fingerprint) != 64 or any(ch not in "0123456789abcdef" for ch in fingerprint):
        raise RuntimeError("media_selection_fingerprint_invalid")
    parent = _selection_root(root)
    entry = parent / fingerprint
    if entry.parent.resolve() != parent.resolve():
        raise RuntimeError("media_selection_path_escape")
    return entry


def _json_copy(value: Any, *, max_bytes: int = 256 * 1024) -> Any:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    if len(encoded) > max_bytes:
        raise RuntimeError("media_selection_json_value_too_large")
    return json.loads(encoded.decode("utf-8"))


def _normalize_asset_id(value: object) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    try:
        return str(int(str(value).strip()))
    except (TypeError, ValueError):
        return str(value)


def _candidate_source_url(candidate: dict, *, provider: str, portrait: bool) -> str:
    review = orchestrator.review_file(candidate, portrait=portrait)
    if not isinstance(review, dict):
        raise RuntimeError("media_selection_candidate_has_no_exact_review_variant")
    source_url = str(review.get("link") or "").strip()
    if not source_url:
        raise RuntimeError("media_selection_candidate_has_no_exact_review_url")
    media_trust._validate_provider_url(provider, source_url)
    return source_url


def _store_selected_raw_from_trust(
    *, root: Path, provider: str, source_url: str
) -> tuple[str, str]:
    record = media_trust._records_by_url.get((provider, source_url))
    if record is None:
        raise RuntimeError("media_selection_missing_live_trust_record")
    if record.provider != provider or record.source_url != source_url:
        raise RuntimeError("media_selection_live_trust_identity_mismatch")
    media_trust._validate_provider_url(provider, source_url)
    media_trust._validate_provider_url(provider, record.final_url)
    source = Path(record.quarantine_path)
    if source.is_symlink() or not source.is_file():
        raise RuntimeError("media_selection_trusted_source_missing")
    if source.stat().st_size != record.byte_length or _sha256_file(source) != record.sha256:
        raise RuntimeError("media_selection_trusted_source_digest_mismatch")

    fingerprint, semantic = asset_cache.raw_fingerprint(
        provider=provider, source_url=source_url
    )
    manifest = {
        "schema_version": asset_cache.SCHEMA_VERSION,
        "namespace": asset_cache.CACHE_NAMESPACE,
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
    asset_cache._store_entry(
        root=root,
        kind="raw",
        fingerprint=fingerprint,
        manifest=manifest,
        source=source,
    )
    return fingerprint, record.sha256


def _validate_raw_reference(
    *, root: Path, provider: str, source_url: str, raw_sha256: str
) -> bool:
    try:
        fingerprint, semantic = asset_cache.raw_fingerprint(
            provider=provider, source_url=source_url
        )
        entry = asset_cache._entry_dir(root, "raw", fingerprint)
        if not entry.is_dir() or entry.is_symlink():
            return False
        manifest, _payload = asset_cache._read_manifest(
            entry, fingerprint=fingerprint, kind="raw"
        )
        if manifest.get("semantic_contract") != semantic:
            return False
        if manifest.get("payload_sha256") != raw_sha256:
            return False
        if manifest.get("provider") != provider or manifest.get("source_url") != source_url:
            return False
        media_trust._validate_provider_url(provider, source_url)
        media_trust._validate_provider_url(provider, str(manifest.get("final_url") or ""))
        return True
    except Exception:
        return False


def _review_context_key(provider: str, candidate: dict) -> tuple[str, str, int]:
    return (
        str(provider).strip().casefold(),
        _normalize_asset_id(candidate.get("id")),
        id(candidate),
    )


def _explicitly_excluded(
    provider: str, asset_id: object, exclude_ids: dict[str, set[int]] | None
) -> bool:
    values = (exclude_ids or {}).get(provider, set())
    if asset_id in values:
        return True
    try:
        return int(str(asset_id).strip()) in values
    except (TypeError, ValueError):
        return False


def _selected_item(
    review: Any,
    *,
    root: Path,
    portrait: bool,
    audit_intended_visual: str,
    slot: str | None,
    seconds: float,
) -> dict[str, Any]:
    provider = str(review.provider).strip().casefold()
    candidate = _json_copy(review.candidate)
    audit = _json_copy(review.audit)
    if not isinstance(candidate, dict) or not isinstance(audit, dict):
        raise RuntimeError("media_selection_review_not_json_object")
    if audit.get("status") != "pass":
        raise RuntimeError("media_selection_selected_review_not_pass")
    source_url = _candidate_source_url(
        candidate, provider=provider, portrait=portrait
    )
    raw_fingerprint, raw_sha = _store_selected_raw_from_trust(
        root=root, provider=provider, source_url=source_url
    )
    return {
        "provider": provider,
        "asset_id": _normalize_asset_id(candidate.get("id")),
        "candidate": candidate,
        "audit": audit,
        "audit_intended_visual": str(audit_intended_visual).strip(),
        "slot": slot,
        "seconds": f"{float(seconds):.6f}",
        "source_url": source_url,
        "raw_fingerprint": raw_fingerprint,
        "raw_sha256": raw_sha,
    }


def _write_selection_record(
    *, root: Path, semantic: dict[str, Any], items: list[dict[str, Any]], metadata: dict[str, Any]
) -> None:
    fingerprint = _selection_fingerprint(semantic)
    entry = _entry_dir(root, fingerprint)
    temp = entry.parent / f".{fingerprint}.tmp-{os.getpid()}-{time.time_ns()}"
    try:
        temp.mkdir(parents=False, exist_ok=False)
        document = sign_document(
            {
                "schema_version": SCHEMA_VERSION,
                "namespace": NAMESPACE,
                "fingerprint": fingerprint,
                "semantic_contract": semantic,
                "items": items,
                "metadata": metadata,
                "created_unix": int(time.time()),
            }
        )
        encoded = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if len(encoded.encode("utf-8")) > MAX_SELECTION_RECORD_BYTES:
            raise RuntimeError("media_selection_record_too_large")
        path = temp / SELECTION_FILENAME
        path.write_text(encoded, encoding="utf-8")
        with path.open("rb") as handle:
            os.fsync(handle.fileno())
        if entry.exists() or entry.is_symlink():
            if entry.is_symlink():
                entry.unlink(missing_ok=True)
            else:
                shutil.rmtree(entry)
        os.replace(temp, entry)
    except Exception:
        shutil.rmtree(temp, ignore_errors=True)
        raise
    _prune_selection_records(root)


def _load_selection_record(
    *, root: Path, semantic: dict[str, Any]
) -> dict[str, Any] | None:
    fingerprint = _selection_fingerprint(semantic)
    entry = _entry_dir(root, fingerprint)
    if not entry.exists():
        return None
    if entry.is_symlink() or not entry.is_dir():
        raise RuntimeError("media_selection_entry_invalid")
    path = entry / SELECTION_FILENAME
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_SELECTION_RECORD_BYTES:
        raise RuntimeError("media_selection_record_file_invalid")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError("media_selection_record_invalid_json") from exc
    verify_document(document)
    if document.get("schema_version") != SCHEMA_VERSION or document.get("namespace") != NAMESPACE:
        raise RuntimeError("media_selection_record_version_mismatch")
    if document.get("fingerprint") != fingerprint:
        raise RuntimeError("media_selection_record_fingerprint_mismatch")
    if document.get("semantic_contract") != semantic:
        raise RuntimeError("media_selection_record_semantic_mismatch")
    items = document.get("items")
    if not isinstance(items, list) or not items:
        raise RuntimeError("media_selection_record_items_invalid")
    return document


def _validate_item_for_reuse(
    item: dict[str, Any],
    *,
    root: Path,
    portrait: bool,
    cache: Any,
    exclude_ids: dict[str, set[int]] | None,
) -> bool:
    try:
        provider = str(item.get("provider") or "").strip().casefold()
        candidate = item.get("candidate")
        audit = item.get("audit")
        if not provider or not isinstance(candidate, dict) or not isinstance(audit, dict):
            return False
        if audit.get("status") != "pass":
            return False
        asset_id = candidate.get("id")
        if _normalize_asset_id(asset_id) != item.get("asset_id"):
            return False
        if cache.unavailable(provider, asset_id) or _explicitly_excluded(
            provider, asset_id, exclude_ids
        ):
            return False
        source_url = str(item.get("source_url") or "").strip()
        if _candidate_source_url(candidate, provider=provider, portrait=portrait) != source_url:
            return False
        raw_sha = str(item.get("raw_sha256") or "")
        if len(raw_sha) != 64:
            return False
        if not _validate_raw_reference(
            root=root,
            provider=provider,
            source_url=source_url,
            raw_sha256=raw_sha,
        ):
            return False
        return True
    except Exception:
        return False


def _cached_review(item: dict[str, Any]) -> Any:
    from isco_video_agent.visual_selection import CandidateReview

    audit = _json_copy(item["audit"])
    audit["intended_visual"] = str(item.get("audit_intended_visual") or "")
    audit["durable_cache_reused"] = True
    audit["durable_cache_origin"] = "authenticated_prior_visual_selection"
    return CandidateReview(
        provider=str(item["provider"]),
        candidate=_json_copy(item["candidate"]),
        audit=audit,
        from_cache=True,
    )


def _restore_result(
    kind: str,
    *,
    semantic: dict[str, Any],
    portrait: bool,
    seconds: float,
    cache: Any,
    exclude_ids: dict[str, set[int]] | None,
) -> Any | None:
    root = asset_cache._configured_cache_root()
    if root is None:
        return None
    try:
        document = _load_selection_record(root=root, semantic=semantic)
        if document is None:
            return None
        items = document["items"]
        if not all(
            isinstance(item, dict)
            and _validate_item_for_reuse(
                item,
                root=root,
                portrait=portrait,
                cache=cache,
                exclude_ids=exclude_ids,
            )
            for item in items
        ):
            raise RuntimeError("media_selection_record_item_validation_failed")

        reviews = [_cached_review(item) for item in items]
        for review in reviews:
            cache.mark_selected(review.provider, review.candidate.get("id"))

        if kind == "single":
            from isco_video_agent.visual_selection import VisualSelectionResult

            if len(reviews) != 1:
                raise RuntimeError("media_selection_single_item_count_invalid")
            metadata = document.get("metadata") or {}
            return VisualSelectionResult(
                status="selected",
                chosen=reviews[0],
                reviewed=reviews,
                used_alternate_query=bool(metadata.get("used_alternate_query")),
                alternate_query=metadata.get("alternate_query"),
            )

        if kind == "opening":
            from isco_video_agent.opening_director import (
                OpeningSequenceResult,
                OpeningSlotSelection,
                opening_slot_specs,
            )

            specs = opening_slot_specs(seconds)
            by_slot = {str(item.get("slot")): (item, review) for item, review in zip(items, reviews)}
            if len(specs) != len(items) or any(spec.key not in by_slot for spec in specs):
                raise RuntimeError("media_selection_opening_slots_changed")
            slots = []
            for spec in specs:
                item, review = by_slot[spec.key]
                if abs(float(item["seconds"]) - float(spec.seconds)) > 0.001:
                    raise RuntimeError("media_selection_opening_duration_changed")
                slots.append(OpeningSlotSelection(spec=spec, review=review))
            return OpeningSequenceResult("selected", slots, reviews)

        if kind == "section_sequence":
            from isco_video_agent.section_visual_sequence import (
                SectionSequenceResult,
                SectionSlotSelection,
                section_slot_specs,
            )

            specs = section_slot_specs(seconds)
            by_slot = {str(item.get("slot")): (item, review) for item, review in zip(items, reviews)}
            if len(specs) != len(items) or any(spec.key not in by_slot for spec in specs):
                raise RuntimeError("media_selection_section_slots_changed")
            slots = []
            for spec in specs:
                item, review = by_slot[spec.key]
                if abs(float(item["seconds"]) - float(spec.seconds)) > 0.001:
                    raise RuntimeError("media_selection_section_duration_changed")
                slots.append(SectionSlotSelection(spec=spec, review=review))
            return SectionSequenceResult("selected", slots, reviews)
        return None
    except Exception as exc:
        fingerprint = _selection_fingerprint(semantic)
        entry = _entry_dir(root, fingerprint)
        if entry.exists() or entry.is_symlink():
            if entry.is_symlink():
                entry.unlink(missing_ok=True)
            else:
                shutil.rmtree(entry, ignore_errors=True)
        print(f"Durable visual selection invalidated ({type(exc).__name__})")
        return None


def _persist_result(
    kind: str,
    result: Any,
    *,
    semantic: dict[str, Any],
    portrait: bool,
    seconds: float,
    intended_visual: str,
    audit_contexts: dict[tuple[str, str, int], str],
) -> None:
    if getattr(result, "status", None) != "selected":
        return
    root = asset_cache._configured_cache_root()
    if root is None:
        return

    if kind == "single":
        reviews_with_slot = [(result.chosen, None, seconds)]
        metadata = {
            "used_alternate_query": bool(getattr(result, "used_alternate_query", False)),
            "alternate_query": getattr(result, "alternate_query", None),
        }
    else:
        reviews_with_slot = [
            (slot.review, slot.spec.key, slot.spec.seconds) for slot in result.slots
        ]
        metadata = {}

    items: list[dict[str, Any]] = []
    for review, slot, item_seconds in reviews_with_slot:
        if review is None:
            raise RuntimeError("media_selection_selected_review_missing")
        context = audit_contexts.get(
            _review_context_key(review.provider, review.candidate), intended_visual
        )
        items.append(
            _selected_item(
                review,
                root=root,
                portrait=portrait,
                audit_intended_visual=context,
                slot=slot,
                seconds=item_seconds,
            )
        )
    _write_selection_record(
        root=root, semantic=semantic, items=items, metadata=metadata
    )


def _wrap_selection(kind: str, original: Callable[..., Any]) -> Callable[..., Any]:
    def wrapped(candidates_by_provider, *args, **kwargs):
        narration_context = str(kwargs.get("narration_context") or "")
        intended_visual = str(kwargs.get("intended_visual") or "")
        portrait = bool(kwargs.get("portrait"))
        seconds_key = "target_seconds" if kind == "single" else "section_seconds"
        seconds = float(kwargs.get(seconds_key) or 0.0)
        cache = kwargs.get("cache")
        exclude_ids = kwargs.get("exclude_ids")
        if cache is None or not str(os.environ.get("ISCO_MEDIA_CACHE_DIR") or "").strip():
            return original(candidates_by_provider, *args, **kwargs)

        semantic = _semantic_contract(
            kind=kind,
            narration_context=narration_context,
            intended_visual=intended_visual,
            portrait=portrait,
            seconds=seconds,
        )
        restored = _restore_result(
            kind,
            semantic=semantic,
            portrait=portrait,
            seconds=seconds,
            cache=cache,
            exclude_ids=exclude_ids,
        )
        if restored is not None:
            print(f"Durable visual selection HIT: kind={kind}")
            return restored

        original_audit = kwargs.get("audit_fn")
        if original_audit is None:
            return original(candidates_by_provider, *args, **kwargs)
        audit_contexts: dict[tuple[str, str, int], str] = {}

        def audit_with_context(
            *, provider: str, candidate: dict, narration_context: str, intended_visual: str
        ) -> dict:
            audit_contexts[_review_context_key(provider, candidate)] = intended_visual
            return original_audit(
                provider=provider,
                candidate=candidate,
                narration_context=narration_context,
                intended_visual=intended_visual,
            )

        call_kwargs = dict(kwargs)
        call_kwargs["audit_fn"] = audit_with_context
        result = original(candidates_by_provider, *args, **call_kwargs)
        try:
            _persist_result(
                kind,
                result,
                semantic=semantic,
                portrait=portrait,
                seconds=seconds,
                intended_visual=intended_visual,
                audit_contexts=audit_contexts,
            )
            if getattr(result, "status", None) == "selected":
                print(f"Durable visual selection STORED: kind={kind}")
        except Exception as exc:
            print(f"Durable visual selection store skipped ({type(exc).__name__})")
        return result

    wrapped._isco_media_durable_selection_resume = True
    wrapped._isco_media_durable_selection_original = original
    return wrapped


def install_media_durable_selection_resume() -> None:
    """Resume a previously verified selected-shot decision without replaying Vision."""
    global _original_single, _original_opening, _original_section
    current_single = orchestrator.select_with_recovery
    if getattr(current_single, "_isco_media_durable_selection_resume", False) is not True:
        _original_single = current_single
        orchestrator.select_with_recovery = _wrap_selection("single", current_single)

    current_opening = orchestrator.select_opening_sequence
    if getattr(current_opening, "_isco_media_durable_selection_resume", False) is not True:
        _original_opening = current_opening
        orchestrator.select_opening_sequence = _wrap_selection("opening", current_opening)

    current_section = orchestrator.select_section_sequence
    if getattr(current_section, "_isco_media_durable_selection_resume", False) is not True:
        _original_section = current_section
        orchestrator.select_section_sequence = _wrap_selection(
            "section_sequence", current_section
        )


def _prune_selection_records(root: Path) -> None:
    parent = root / SELECTION_DIRNAME
    if not parent.exists():
        return
    if parent.is_symlink():
        parent.unlink(missing_ok=True)
        return
    entries: list[tuple[float, Path]] = []
    for entry in list(parent.iterdir()):
        if entry.is_symlink() or not entry.is_dir():
            if entry.is_symlink():
                entry.unlink(missing_ok=True)
            elif entry.exists():
                entry.unlink(missing_ok=True)
            continue
        try:
            stamp = entry.stat().st_mtime
        except OSError:
            stamp = 0.0
        entries.append((stamp, entry))
    entries.sort(key=lambda item: item[0], reverse=True)
    for _stamp, entry in entries[MAX_SELECTION_RECORDS:]:
        shutil.rmtree(entry, ignore_errors=True)


def prepare_selection_cache_for_persistence(path: Path) -> int:
    """Authenticate and validate decision records before the Actions cache is saved."""
    root = Path(path)
    if root.exists() and root.is_symlink():
        return 0
    root = root.resolve()
    parent = root / SELECTION_DIRNAME
    if not parent.exists():
        return 0
    if parent.is_symlink():
        parent.unlink(missing_ok=True)
        return 0
    valid = 0
    current_contract = _selection_contract_sha256()
    current_model = str(os.environ.get("GEMINI_CONTENT_MODEL") or "").strip()
    for entry in list(parent.iterdir()):
        remove = False
        try:
            if entry.is_symlink() or not entry.is_dir() or len(entry.name) != 64:
                raise RuntimeError("selection_entry_invalid")
            record_path = entry / SELECTION_FILENAME
            if (
                record_path.is_symlink()
                or not record_path.is_file()
                or record_path.stat().st_size > MAX_SELECTION_RECORD_BYTES
            ):
                raise RuntimeError("selection_record_invalid")
            document = json.loads(record_path.read_text(encoding="utf-8"))
            verify_document(document)
            semantic = document.get("semantic_contract")
            if not isinstance(semantic, dict):
                raise RuntimeError("selection_semantic_invalid")
            if document.get("fingerprint") != entry.name:
                raise RuntimeError("selection_fingerprint_invalid")
            if semantic.get("selection_contract_sha256") != current_contract:
                raise RuntimeError("selection_contract_stale")
            if current_model and semantic.get("content_model") != current_model:
                raise RuntimeError("selection_model_stale")
            items = document.get("items")
            if not isinstance(items, list) or not items:
                raise RuntimeError("selection_items_invalid")
            for item in items:
                if not isinstance(item, dict) or not _validate_raw_reference(
                    root=root,
                    provider=str(item.get("provider") or "").strip().casefold(),
                    source_url=str(item.get("source_url") or "").strip(),
                    raw_sha256=str(item.get("raw_sha256") or ""),
                ):
                    raise RuntimeError("selection_raw_reference_invalid")
            valid += 1
        except Exception:
            remove = True
        if remove:
            if entry.is_symlink():
                entry.unlink(missing_ok=True)
            else:
                shutil.rmtree(entry, ignore_errors=True)
    _prune_selection_records(root)
    print(f"Media durable selection cache sanitized: valid_records={valid}")
    return valid

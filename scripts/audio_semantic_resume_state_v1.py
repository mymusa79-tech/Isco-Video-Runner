from __future__ import annotations

"""Persist/restore Audio Semantic Integrity provenance without replaying production.

Long-form Audio Semantic Integrity normally lives in process memory because it binds the
exact TTS section bytes -> narration concat -> authorized mux chain.  An Audio Production
V2 provider-availability failure happens *after* that provenance has already been proved
and after final.mp4 exists, but a later workflow process would otherwise lose the in-memory
bindings.  This module serializes only those immutable bindings and validates every path,
hash and byte count before restoring them.

Moment/Short production deliberately has no Engine narration provenance final gate; its
finished Short voice is verified by Audio Production V2 instead.  For Moment we therefore
persist only exact final identity and production identity, never fabricate TTS bindings.
"""

import hashlib
import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from scripts import audio_semantic_integrity as integrity


CONTRACT_ID = "audio.semantic.resume-state.v1"
SCHEMA_VERSION = 1
FILENAME = "audio-semantic-resume-state.json"


class AudioSemanticResumeStateError(RuntimeError):
    pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise AudioSemanticResumeStateError(f"audio_semantic_resume_invalid_json:{Path(path).name}") from exc
    if not isinstance(value, dict):
        raise AudioSemanticResumeStateError(f"audio_semantic_resume_wrong_shape:{Path(path).name}")
    return value


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
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


def _safe_relative(root: Path, path: str | Path) -> str:
    root = Path(root).resolve()
    candidate = Path(path)
    try:
        resolved = candidate.resolve(strict=True)
        relative = resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise AudioSemanticResumeStateError("audio_semantic_resume_path_escaped_output_root") from exc
    if candidate.is_symlink() or resolved.is_symlink():
        raise AudioSemanticResumeStateError("audio_semantic_resume_symlink_forbidden")
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise AudioSemanticResumeStateError("audio_semantic_resume_relative_path_invalid")
    return relative.as_posix()


def _resolve_evidence_file(root: Path, relative: object, *, sha256: object, byte_length: object) -> Path:
    rel = str(relative or "").strip()
    if not rel or Path(rel).is_absolute() or ".." in Path(rel).parts:
        raise AudioSemanticResumeStateError("audio_semantic_resume_evidence_path_invalid")
    root_resolved = Path(root).resolve()
    candidate = root_resolved / rel
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root_resolved)
    except (OSError, ValueError) as exc:
        raise AudioSemanticResumeStateError("audio_semantic_resume_evidence_path_escaped") from exc
    if candidate.is_symlink() or resolved.is_symlink() or not resolved.is_file():
        raise AudioSemanticResumeStateError("audio_semantic_resume_evidence_file_invalid")
    expected_sha = str(sha256 or "").strip().lower()
    if len(expected_sha) != 64 or _sha256_file(resolved) != expected_sha:
        raise AudioSemanticResumeStateError(f"audio_semantic_resume_hash_mismatch:{rel}")
    try:
        expected_bytes = int(byte_length)
    except (TypeError, ValueError) as exc:
        raise AudioSemanticResumeStateError(f"audio_semantic_resume_size_invalid:{rel}") from exc
    if expected_bytes <= 0 or resolved.stat().st_size != expected_bytes:
        raise AudioSemanticResumeStateError(f"audio_semantic_resume_size_mismatch:{rel}")
    return resolved


def _plan_format(root: Path) -> tuple[str, dict[str, Any]]:
    plan = _read_object(Path(root) / "plan.json")
    fmt = str(plan.get("format") or "").strip().lower()
    if fmt not in {"film", "story", "moment"}:
        raise AudioSemanticResumeStateError(f"audio_semantic_resume_format_invalid:{fmt or 'missing'}")
    return fmt, plan


def export_audio_semantic_resume_state(output_dir: Path) -> dict[str, Any]:
    """Write immutable provenance needed to replay only the post-render final gates."""
    root = Path(output_dir).resolve()
    final_path = root / "final.mp4"
    if not final_path.is_file() or final_path.stat().st_size <= 1024:
        raise AudioSemanticResumeStateError("audio_semantic_resume_final_missing")
    fmt, plan = _plan_format(root)
    production_id = str(getattr(integrity, "_production_id", "") or "").strip()
    env_production_id = str(os.environ.get("ISCO_PRODUCTION_ID") or "").strip()
    if not production_id or production_id != env_production_id:
        raise AudioSemanticResumeStateError("audio_semantic_resume_production_identity_unbound")

    final_sha = _sha256_file(final_path)
    document: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "contract_id": CONTRACT_ID,
        "production_id": production_id,
        "format": fmt,
        "final": {
            "path": "final.mp4",
            "sha256": final_sha,
            "bytes": final_path.stat().st_size,
        },
        "media_rebuild_allowed": False,
        "tts_replay_allowed": False,
    }

    if fmt == "moment":
        document.update(
            {
                "mode": "moment_final_gate_not_applicable",
                "tts_sections": [],
                "narration": None,
                "final_mux": None,
            }
        )
        _atomic_json(root / FILENAME, document)
        return document

    sections = plan.get("sections")
    if not isinstance(sections, list) or not sections:
        raise AudioSemanticResumeStateError("audio_semantic_resume_long_sections_missing")
    expected_tasks = [f"TTS_SECTION_{index:02d}" for index in range(1, len(sections) + 1)]
    actual_tasks = list(getattr(integrity, "_tts_by_task", {}))
    if actual_tasks != expected_tasks:
        raise AudioSemanticResumeStateError("audio_semantic_resume_tts_task_order_mismatch")

    tts_sections: list[dict[str, Any]] = []
    for task_id in expected_tasks:
        record = integrity._tts_by_task.get(task_id)
        if record is None:
            raise AudioSemanticResumeStateError(f"audio_semantic_resume_tts_binding_missing:{task_id}")
        source_path = Path(record.audio_path)
        relative = _safe_relative(root, source_path)
        if _sha256_file(source_path) != record.audio_sha256 or source_path.stat().st_size != record.audio_bytes:
            raise AudioSemanticResumeStateError(f"audio_semantic_resume_tts_binding_changed:{task_id}")
        item = asdict(record)
        item["audio_path"] = relative
        tts_sections.append(item)

    narration_bindings = getattr(integrity, "_narration_by_path", {})
    if len(narration_bindings) != 1:
        raise AudioSemanticResumeStateError("audio_semantic_resume_narration_binding_count_invalid")
    narration = next(iter(narration_bindings.values()))
    narration_path = Path(narration.path)
    narration_relative = _safe_relative(root, narration_path)
    if _sha256_file(narration_path) != narration.sha256 or narration_path.stat().st_size != narration.byte_length:
        raise AudioSemanticResumeStateError("audio_semantic_resume_narration_binding_changed")
    if list(narration.ordered_task_ids) != expected_tasks:
        raise AudioSemanticResumeStateError("audio_semantic_resume_narration_task_order_mismatch")
    narration_doc = asdict(narration)
    narration_doc["path"] = narration_relative
    narration_doc["ordered_task_ids"] = list(narration.ordered_task_ids)
    narration_doc["ordered_transcript_sha256"] = list(narration.ordered_transcript_sha256)
    narration_doc["ordered_audio_sha256"] = list(narration.ordered_audio_sha256)

    final_record = getattr(integrity, "_final_by_path", {}).get(integrity._path_key(final_path))
    if final_record is None:
        raise AudioSemanticResumeStateError("audio_semantic_resume_final_mux_binding_missing")
    final_record_path = Path(final_record.final_path)
    final_relative = _safe_relative(root, final_record_path)
    if final_relative != "final.mp4" or final_record.final_sha256 != final_sha:
        raise AudioSemanticResumeStateError("audio_semantic_resume_final_mux_identity_mismatch")
    if final_record.final_bytes != final_path.stat().st_size:
        raise AudioSemanticResumeStateError("audio_semantic_resume_final_mux_size_mismatch")
    final_mux_doc = asdict(final_record)
    final_mux_doc["final_path"] = final_relative
    final_mux_doc["narration_path"] = narration_relative

    document.update(
        {
            "mode": "long_provenance_replay",
            "tts_sections": tts_sections,
            "narration": narration_doc,
            "final_mux": final_mux_doc,
        }
    )
    _atomic_json(root / FILENAME, document)
    return document


def restore_audio_semantic_resume_state(
    output_dir: Path,
    *,
    expected_production_id: str,
    state_path: Path | None = None,
) -> dict[str, Any]:
    """Restore only validated in-memory bindings; never synthesize or mutate media."""
    root = Path(output_dir).resolve()
    document = _read_object(state_path or (root / FILENAME))
    if document.get("contract_id") != CONTRACT_ID or int(document.get("schema_version") or 0) != SCHEMA_VERSION:
        raise AudioSemanticResumeStateError("audio_semantic_resume_contract_mismatch")
    production_id = str(document.get("production_id") or "").strip()
    if not expected_production_id or production_id != expected_production_id:
        raise AudioSemanticResumeStateError("audio_semantic_resume_source_production_mismatch")
    fmt, plan = _plan_format(root)
    if str(document.get("format") or "").strip().lower() != fmt:
        raise AudioSemanticResumeStateError("audio_semantic_resume_format_mismatch")

    final_doc = document.get("final")
    if not isinstance(final_doc, dict) or str(final_doc.get("path") or "") != "final.mp4":
        raise AudioSemanticResumeStateError("audio_semantic_resume_final_evidence_missing")
    final_path = _resolve_evidence_file(
        root,
        "final.mp4",
        sha256=final_doc.get("sha256"),
        byte_length=final_doc.get("bytes"),
    )

    integrity.reset_audio_semantic_integrity_state_for_tests()
    integrity._production_id = production_id
    os.environ["ISCO_PRODUCTION_ID"] = production_id

    if fmt == "moment":
        if document.get("mode") != "moment_final_gate_not_applicable":
            raise AudioSemanticResumeStateError("audio_semantic_resume_moment_mode_invalid")
        if list(document.get("tts_sections") or []) or document.get("narration") is not None or document.get("final_mux") is not None:
            raise AudioSemanticResumeStateError("audio_semantic_resume_moment_fabricated_provenance")
        return document

    if document.get("mode") != "long_provenance_replay":
        raise AudioSemanticResumeStateError("audio_semantic_resume_long_mode_invalid")
    sections = plan.get("sections")
    if not isinstance(sections, list) or not sections:
        raise AudioSemanticResumeStateError("audio_semantic_resume_long_sections_missing")
    expected_tasks = [f"TTS_SECTION_{index:02d}" for index in range(1, len(sections) + 1)]
    items = document.get("tts_sections")
    if not isinstance(items, list) or [str(item.get("task_id") or "") for item in items if isinstance(item, dict)] != expected_tasks:
        raise AudioSemanticResumeStateError("audio_semantic_resume_tts_state_order_invalid")

    transcript_hashes: list[str] = []
    audio_hashes: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            raise AudioSemanticResumeStateError("audio_semantic_resume_tts_state_invalid")
        path = _resolve_evidence_file(
            root,
            item.get("audio_path"),
            sha256=item.get("audio_sha256"),
            byte_length=item.get("audio_bytes"),
        )
        record = integrity.TtsSectionBinding(
            task_id=str(item.get("task_id") or ""),
            transcript_sha256=str(item.get("transcript_sha256") or ""),
            transcript_utf8_bytes=int(item.get("transcript_utf8_bytes") or 0),
            audio_path=integrity._path_key(path),
            audio_sha256=str(item.get("audio_sha256") or "").lower(),
            audio_bytes=int(item.get("audio_bytes") or 0),
        )
        if len(record.transcript_sha256) != 64 or record.transcript_utf8_bytes <= 0:
            raise AudioSemanticResumeStateError(f"audio_semantic_resume_tts_metadata_invalid:{record.task_id}")
        integrity._tts_by_task[record.task_id] = record
        integrity._tts_by_path[record.audio_path] = record
        transcript_hashes.append(record.transcript_sha256)
        audio_hashes.append(record.audio_sha256)

    narration_doc = document.get("narration")
    if not isinstance(narration_doc, dict):
        raise AudioSemanticResumeStateError("audio_semantic_resume_narration_state_missing")
    narration_path = _resolve_evidence_file(
        root,
        narration_doc.get("path"),
        sha256=narration_doc.get("sha256"),
        byte_length=narration_doc.get("byte_length"),
    )
    ordered_tasks = tuple(str(value) for value in list(narration_doc.get("ordered_task_ids") or []))
    ordered_transcripts = tuple(str(value) for value in list(narration_doc.get("ordered_transcript_sha256") or []))
    ordered_audio = tuple(str(value) for value in list(narration_doc.get("ordered_audio_sha256") or []))
    if list(ordered_tasks) != expected_tasks or list(ordered_transcripts) != transcript_hashes or list(ordered_audio) != audio_hashes:
        raise AudioSemanticResumeStateError("audio_semantic_resume_narration_chain_mismatch")
    narration = integrity.NarrationBinding(
        path=integrity._path_key(narration_path),
        sha256=str(narration_doc.get("sha256") or "").lower(),
        byte_length=int(narration_doc.get("byte_length") or 0),
        ordered_task_ids=ordered_tasks,
        ordered_transcript_sha256=ordered_transcripts,
        ordered_audio_sha256=ordered_audio,
        authorized_transform_sha256=str(narration_doc.get("authorized_transform_sha256") or ""),
    )
    if len(narration.authorized_transform_sha256) != 64:
        raise AudioSemanticResumeStateError("audio_semantic_resume_narration_transform_invalid")
    integrity._narration_by_path[narration.path] = narration

    mux_doc = document.get("final_mux")
    if not isinstance(mux_doc, dict):
        raise AudioSemanticResumeStateError("audio_semantic_resume_final_mux_state_missing")
    if str(mux_doc.get("final_path") or "") != "final.mp4":
        raise AudioSemanticResumeStateError("audio_semantic_resume_final_mux_path_invalid")
    if str(mux_doc.get("narration_path") or "") != str(narration_doc.get("path") or ""):
        raise AudioSemanticResumeStateError("audio_semantic_resume_final_mux_narration_path_mismatch")
    mux = integrity.FinalMuxBinding(
        final_path=integrity._path_key(final_path),
        final_sha256=str(mux_doc.get("final_sha256") or "").lower(),
        final_bytes=int(mux_doc.get("final_bytes") or 0),
        narration_path=narration.path,
        narration_sha256=str(mux_doc.get("narration_sha256") or "").lower(),
        authorized_mux_chain_sha256=str(mux_doc.get("authorized_mux_chain_sha256") or ""),
    )
    if mux.final_sha256 != str(final_doc.get("sha256") or "").lower() or mux.final_bytes != final_path.stat().st_size:
        raise AudioSemanticResumeStateError("audio_semantic_resume_final_mux_current_identity_mismatch")
    if mux.narration_sha256 != narration.sha256 or len(mux.authorized_mux_chain_sha256) != 64:
        raise AudioSemanticResumeStateError("audio_semantic_resume_final_mux_chain_invalid")
    integrity._final_by_path[mux.final_path] = mux
    return document

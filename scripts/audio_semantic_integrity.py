from __future__ import annotations

import contextlib
import hashlib
import inspect
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

import isco_video_agent.orchestrator as orchestrator


AUDIT_FILENAME = "audio-semantic-integrity.json"
SCHEMA_VERSION = 1
_INSTALLED_BINDING = False
_INSTALLED_FINAL_GATE = False
_production_id = ""


@dataclass(frozen=True)
class TtsSectionBinding:
    task_id: str
    transcript_sha256: str
    transcript_utf8_bytes: int
    audio_path: str
    audio_sha256: str
    audio_bytes: int


@dataclass(frozen=True)
class NarrationBinding:
    path: str
    sha256: str
    byte_length: int
    ordered_task_ids: tuple[str, ...]
    ordered_transcript_sha256: tuple[str, ...]
    ordered_audio_sha256: tuple[str, ...]
    authorized_transform_sha256: str


@dataclass(frozen=True)
class FinalMuxBinding:
    final_path: str
    final_sha256: str
    final_bytes: int
    narration_path: str
    narration_sha256: str
    authorized_mux_chain_sha256: str


_tts_by_task: dict[str, TtsSectionBinding] = {}
_tts_by_path: dict[str, TtsSectionBinding] = {}
_narration_by_path: dict[str, NarrationBinding] = {}
_final_by_path: dict[str, FinalMuxBinding] = {}


def _path_key(path: str | Path) -> str:
    return str(Path(path).resolve())


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_text(text: str) -> str:
    return _sha256_bytes(str(text).encode("utf-8"))


def _sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_size(path: str | Path) -> int:
    return Path(path).stat().st_size


def _implementation_sha256(fn: Callable[..., Any]) -> str:
    try:
        source = inspect.getsource(fn).encode("utf-8")
    except (OSError, TypeError):
        code = getattr(fn, "__code__", None)
        if code is None:
            raise RuntimeError("audio_semantic_integrity_transform_identity_unavailable")
        source = code.co_code
    return _sha256_bytes(source)


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


def reset_audio_semantic_integrity_state_for_tests() -> None:
    global _production_id
    _production_id = ""
    _tts_by_task.clear()
    _tts_by_path.clear()
    _narration_by_path.clear()
    _final_by_path.clear()


def _begin_run() -> None:
    reset_audio_semantic_integrity_state_for_tests()
    global _production_id
    _production_id = str(os.environ.get("ISCO_PRODUCTION_ID") or "").strip()
    if not _production_id:
        raise RuntimeError("audio_semantic_integrity_missing_production_id")


def _arg_or_kw(args: tuple[Any, ...], kwargs: dict[str, Any], position: int, name: str) -> Any:
    if name in kwargs:
        return kwargs[name]
    return args[position] if len(args) > position else None


def _wrap_tts(original: Callable[..., Any]) -> Callable[..., Any]:
    def wrapped(*args, **kwargs):
        task_id = str(_arg_or_kw(args, kwargs, 3, "task_id") or "").strip()
        transcript = _arg_or_kw(args, kwargs, 5, "transcript")
        output = _arg_or_kw(args, kwargs, 6, "output")
        if not task_id.startswith("TTS_SECTION_"):
            raise RuntimeError("audio_semantic_integrity_unbound_tts_task")
        if not isinstance(transcript, str) or not transcript.strip():
            raise RuntimeError("audio_semantic_integrity_empty_tts_transcript")
        if output is None:
            raise RuntimeError("audio_semantic_integrity_missing_tts_output")
        result = original(*args, **kwargs)
        path = Path(result if result is not None else output)
        if not path.is_file() or path.stat().st_size <= 0:
            raise RuntimeError("audio_semantic_integrity_tts_output_missing")
        record = TtsSectionBinding(
            task_id=task_id,
            transcript_sha256=_sha256_text(transcript),
            transcript_utf8_bytes=len(transcript.encode("utf-8")),
            audio_path=_path_key(path),
            audio_sha256=_sha256_file(path),
            audio_bytes=_file_size(path),
        )
        if task_id in _tts_by_task:
            raise RuntimeError(f"audio_semantic_integrity_duplicate_tts_task:{task_id}")
        _tts_by_task[task_id] = record
        _tts_by_path[record.audio_path] = record
        return result

    return wrapped


def _wrap_concat_audio(original: Callable[..., Any]) -> Callable[..., Any]:
    transform_sha = _implementation_sha256(original)

    def wrapped(inputs, output, *args, **kwargs):
        output_path = Path(output)
        if output_path.name != "narration.wav":
            return original(inputs, output, *args, **kwargs)
        paths = [Path(item) for item in inputs]
        if not paths:
            raise RuntimeError("audio_semantic_integrity_empty_narration_concat")
        records: list[TtsSectionBinding] = []
        for path in paths:
            record = _tts_by_path.get(_path_key(path))
            if record is None:
                raise RuntimeError(f"audio_semantic_integrity_uncertified_concat_input:{path.name}")
            if _sha256_file(path) != record.audio_sha256:
                raise RuntimeError(f"audio_semantic_integrity_tts_audio_changed:{record.task_id}")
            records.append(record)

        result = original(inputs, output, *args, **kwargs)
        narration_path = Path(result if result is not None else output_path)
        for path, record in zip(paths, records):
            if _sha256_file(path) != record.audio_sha256:
                raise RuntimeError(f"audio_semantic_integrity_tts_audio_mutated:{record.task_id}")
        if not narration_path.is_file() or narration_path.stat().st_size <= 0:
            raise RuntimeError("audio_semantic_integrity_narration_missing")
        binding = NarrationBinding(
            path=_path_key(narration_path),
            sha256=_sha256_file(narration_path),
            byte_length=_file_size(narration_path),
            ordered_task_ids=tuple(item.task_id for item in records),
            ordered_transcript_sha256=tuple(item.transcript_sha256 for item in records),
            ordered_audio_sha256=tuple(item.audio_sha256 for item in records),
            authorized_transform_sha256=transform_sha,
        )
        _narration_by_path[binding.path] = binding
        return result

    return wrapped


def _wrap_mux(original: Callable[..., Any]) -> Callable[..., Any]:
    mux_chain_sha = _implementation_sha256(original)

    def wrapped(video, narration, output, *args, **kwargs):
        if narration is None:
            return original(video, narration, output, *args, **kwargs)
        narration_path = Path(narration)
        narration_record = _narration_by_path.get(_path_key(narration_path))
        if narration_record is None:
            raise RuntimeError("audio_semantic_integrity_uncertified_narration_at_mux")
        if _sha256_file(narration_path) != narration_record.sha256:
            raise RuntimeError("audio_semantic_integrity_narration_changed_before_mux")
        result = original(video, narration, output, *args, **kwargs)
        final_path = Path(result if result is not None else output)
        if _sha256_file(narration_path) != narration_record.sha256:
            raise RuntimeError("audio_semantic_integrity_narration_mutated_by_mux_chain")
        if not final_path.is_file() or final_path.stat().st_size <= 0:
            raise RuntimeError("audio_semantic_integrity_final_missing_after_mux")
        binding = FinalMuxBinding(
            final_path=_path_key(final_path),
            final_sha256=_sha256_file(final_path),
            final_bytes=_file_size(final_path),
            narration_path=narration_record.path,
            narration_sha256=narration_record.sha256,
            authorized_mux_chain_sha256=mux_chain_sha,
        )
        _final_by_path[binding.final_path] = binding
        return result

    return wrapped


@contextlib.contextmanager
def _audio_binding_scope() -> Iterator[None]:
    original_tts = orchestrator._synthesize_tts_section
    original_concat = orchestrator.concat_audio
    original_mux = orchestrator.mux
    orchestrator._synthesize_tts_section = _wrap_tts(original_tts)
    orchestrator.concat_audio = _wrap_concat_audio(original_concat)
    orchestrator.mux = _wrap_mux(original_mux)
    try:
        yield
    finally:
        orchestrator._synthesize_tts_section = original_tts
        orchestrator.concat_audio = original_concat
        orchestrator.mux = original_mux


def install_audio_semantic_integrity_binding() -> None:
    """Install before Audio Mastering/SFX so runtime scope wraps their live functions."""
    global _INSTALLED_BINDING
    if _INSTALLED_BINDING:
        return
    current = orchestrator.produce
    if getattr(current, "_isco_audio_semantic_binding", False):
        _INSTALLED_BINDING = True
        return

    def wrapped(*args, **kwargs):
        _begin_run()
        with _audio_binding_scope():
            return current(*args, **kwargs)

    wrapped._isco_audio_semantic_binding = True
    wrapped._isco_audio_semantic_original = current
    orchestrator.produce = wrapped
    _INSTALLED_BINDING = True


def _plan_narrations(plan: dict[str, Any]) -> list[str]:
    sections = plan.get("sections")
    if not isinstance(sections, list):
        raise RuntimeError("audio_semantic_integrity_plan_sections_missing")
    narrations: list[str] = []
    for section in sections:
        if not isinstance(section, dict):
            raise RuntimeError("audio_semantic_integrity_plan_section_invalid")
        text = section.get("narration")
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError("audio_semantic_integrity_plan_narration_missing")
        narrations.append(text)
    if not narrations:
        raise RuntimeError("audio_semantic_integrity_plan_narration_missing")
    return narrations


def _expected_task_ids(count: int) -> list[str]:
    return [f"TTS_SECTION_{index:02d}" for index in range(1, count + 1)]


def require_audio_semantic_integrity(output_dir: Path) -> dict[str, Any]:
    """Fail closed on provenance mismatch; Groq/Whisper remains observe-only."""
    output_dir = Path(output_dir)
    audit_path = output_dir / AUDIT_FILENAME
    document: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "mode": "enforce_binding",
        "production_id": _production_id,
        "decision": "block",
        "groq_semantic_audit": "observe_only",
        "checks": {},
    }
    try:
        if not _production_id:
            raise RuntimeError("audio_semantic_integrity_run_not_started")
        plan = json.loads((output_dir / "plan.json").read_text(encoding="utf-8"))
        if not isinstance(plan, dict):
            raise RuntimeError("audio_semantic_integrity_plan_invalid")
        if str(plan.get("format") or "").strip().lower() == "moment":
            document.update({"decision": "not_applicable", "reason": "moment format has no narration TTS path"})
            _atomic_json(audit_path, document)
            return document

        narrations = _plan_narrations(plan)
        expected_tasks = _expected_task_ids(len(narrations))
        actual_tasks = list(_tts_by_task)
        if actual_tasks != expected_tasks:
            raise RuntimeError(f"audio_semantic_integrity_tts_order_mismatch:expected={expected_tasks},actual={actual_tasks}")
        expected_transcript_hashes = [_sha256_text(text) for text in narrations]
        section_evidence: list[dict[str, Any]] = []
        for task_id, expected_hash in zip(expected_tasks, expected_transcript_hashes):
            record = _tts_by_task[task_id]
            if record.transcript_sha256 != expected_hash:
                raise RuntimeError(f"audio_semantic_integrity_transcript_mismatch:{task_id}")
            audio_path = Path(record.audio_path)
            if not audio_path.is_file() or _sha256_file(audio_path) != record.audio_sha256:
                raise RuntimeError(f"audio_semantic_integrity_section_audio_mismatch:{task_id}")
            section_evidence.append(asdict(record))

        if len(_narration_by_path) != 1:
            raise RuntimeError("audio_semantic_integrity_narration_binding_count_invalid")
        narration_record = next(iter(_narration_by_path.values()))
        narration_path = Path(narration_record.path)
        if list(narration_record.ordered_task_ids) != expected_tasks:
            raise RuntimeError("audio_semantic_integrity_concat_order_mismatch")
        if list(narration_record.ordered_transcript_sha256) != expected_transcript_hashes:
            raise RuntimeError("audio_semantic_integrity_concat_transcript_binding_mismatch")
        if not narration_path.is_file() or _sha256_file(narration_path) != narration_record.sha256:
            raise RuntimeError("audio_semantic_integrity_narration_hash_mismatch")

        final_path = output_dir / "final.mp4"
        final_record = _final_by_path.get(_path_key(final_path))
        if final_record is None:
            raise RuntimeError("audio_semantic_integrity_mux_binding_missing")
        if final_record.narration_sha256 != narration_record.sha256:
            raise RuntimeError("audio_semantic_integrity_mux_narration_mismatch")
        if not final_path.is_file() or _sha256_file(final_path) != final_record.final_sha256:
            raise RuntimeError("audio_semantic_integrity_final_hash_mismatch")

        quality = json.loads((output_dir / "quality-final.json").read_text(encoding="utf-8"))
        if not isinstance(quality, dict):
            raise RuntimeError("audio_semantic_integrity_quality_final_invalid")
        if quality.get("audio_ok") is not True or quality.get("av_sync_ok") is not True:
            raise RuntimeError("audio_semantic_integrity_final_audio_quality_not_certified")

        document.update(
            {
                "decision": "pass",
                "checks": {
                    "approved_plan_to_tts": True,
                    "tts_audio_immutability": True,
                    "ordered_authorized_narration_transform": True,
                    "narration_immutability": True,
                    "certified_narration_handed_to_authorized_mux_chain": True,
                    "final_artifact_immutability": True,
                    "engine_audio_and_av_sync_gates": True,
                },
                "sections": section_evidence,
                "narration": asdict(narration_record),
                "final_mux": asdict(final_record),
            }
        )
        _atomic_json(audit_path, document)
        print("Audio Semantic Integrity PASS: approved narration provenance is intact through final.mp4")
        return document
    except Exception as exc:
        document["error"] = f"{type(exc).__name__}: {str(exc)[:800]}"
        try:
            _atomic_json(audit_path, document)
        finally:
            raise RuntimeError("Audio Semantic Integrity gate blocked production: " + str(exc)) from exc


def install_audio_semantic_final_gate(production_modules: list[Any]) -> None:
    """Run after produce returns and immediately before Final Master QC."""
    global _INSTALLED_FINAL_GATE
    if _INSTALLED_FINAL_GATE:
        return
    installed = 0
    for production in production_modules:
        original = getattr(production, "run_final_master_qc", None)
        if not callable(original) or getattr(original, "_isco_audio_semantic_final_gate", False):
            continue

        def make_wrapper(current):
            def wrapped(output_dir: Path, *args, **kwargs):
                require_audio_semantic_integrity(Path(output_dir))
                return current(output_dir, *args, **kwargs)

            wrapped._isco_audio_semantic_final_gate = True
            wrapped._isco_audio_semantic_original = current
            return wrapped

        production.run_final_master_qc = make_wrapper(original)
        installed += 1
    if installed <= 0:
        raise RuntimeError("audio_semantic_integrity_final_master_qc_binding_missing")
    _INSTALLED_FINAL_GATE = True

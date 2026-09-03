from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


CONTRACT_ID = "final.master.acceptance.v2"
CONTRACT_SCHEMA_VERSION = 2
REPORT_FILENAME = "final-master-qc.json"


class FinalMasterAcceptanceError(RuntimeError):
    pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_file_binding(path: Path) -> dict[str, Any]:
    path = Path(path)
    try:
        if path.is_symlink() or not path.is_file():
            raise FinalMasterAcceptanceError(f"final_master_acceptance_invalid_file:{path.name}")
        size = int(path.stat().st_size)
    except OSError as exc:
        raise FinalMasterAcceptanceError(f"final_master_acceptance_unreadable_file:{path.name}") from exc
    if size <= 0:
        raise FinalMasterAcceptanceError(f"final_master_acceptance_empty_file:{path.name}")
    return {"file": path.name, "sha256": _sha256_file(path), "byte_length": size}


def _read_object(path: Path) -> dict[str, Any]:
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError("not a regular file")
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise FinalMasterAcceptanceError(f"final_master_acceptance_invalid_json:{path.name}") from exc
    if not isinstance(value, dict):
        raise FinalMasterAcceptanceError(f"final_master_acceptance_wrong_shape:{path.name}")
    return value


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path = Path(path)
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


def _source_bindings(root: Path) -> dict[str, dict[str, Any]]:
    return {
        "final": _regular_file_binding(root / "final.mp4"),
        "plan": _regular_file_binding(root / "plan.json"),
        "quality_final": _regular_file_binding(root / "quality-final.json"),
        "visual_timeline": _regular_file_binding(root / "visual-timeline.json"),
    }


def _optional_evidence_bindings(root: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for key, filename in (
        ("audio_production_contract", "audio-production-contract-v2.json"),
        ("audio_producer_repair", "audio-producer-repair.json"),
    ):
        path = root / filename
        if path.is_file() and not path.is_symlink():
            result[key] = _regular_file_binding(path)
    return result


def _implementation_sha256() -> str:
    from scripts import final_master_qc

    raw = getattr(final_master_qc, "__file__", None)
    if not raw:
        raise FinalMasterAcceptanceError("final_master_acceptance_missing_implementation")
    return _sha256_file(Path(raw).resolve())


def seal_final_master_acceptance(
    output_dir: Path,
    report: dict[str, Any],
    *,
    policy_fingerprint: str,
) -> dict[str, Any]:
    """Atomically turn final-master-qc.json into the exact-artifact P4 receipt."""
    root = Path(output_dir)
    if report.get("status") not in {"pass", "block"}:
        raise FinalMasterAcceptanceError("final_master_acceptance_invalid_qc_decision")
    if len(str(policy_fingerprint or "")) != 64:
        raise FinalMasterAcceptanceError("final_master_acceptance_invalid_policy_fingerprint")

    sources = _source_bindings(root)
    acceptance = {
        "contract_id": CONTRACT_ID,
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "decision": report.get("status"),
        "production_stage": "post_render_pre_gold_acceptance",
        "qc_policy_fingerprint": policy_fingerprint,
        "implementation_sha256": _implementation_sha256(),
        "runner_sha": (os.environ.get("GITHUB_SHA") or "").strip().lower() or None,
        "engine_sha": (os.environ.get("ISCO_ENGINE_SHA") or "").strip().lower() or None,
        "sources": sources,
        "upstream_evidence": _optional_evidence_bindings(root),
    }
    sealed = dict(report)
    sealed["acceptance_contract"] = acceptance
    _atomic_json(root / REPORT_FILENAME, sealed)
    return sealed


def require_final_master_acceptance(
    output_dir: Path,
    *,
    report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fail closed unless the P4 PASS receipt belongs to the current exact artifacts."""
    root = Path(output_dir)
    document = dict(report) if isinstance(report, dict) else _read_object(root / REPORT_FILENAME)
    if not (
        document.get("status") == "pass"
        and document.get("production_stage") == "post_render_pre_gold_acceptance"
        and document.get("full_decode_ok") is True
        and document.get("full_decode_timed_out") is False
        and document.get("final_media_mutated") is False
        and not list(document.get("blocking_findings") or [])
    ):
        raise FinalMasterAcceptanceError("final_master_acceptance_qc_not_pass")

    acceptance = document.get("acceptance_contract")
    if not isinstance(acceptance, dict):
        raise FinalMasterAcceptanceError("final_master_acceptance_missing_contract")
    if acceptance.get("contract_id") != CONTRACT_ID:
        raise FinalMasterAcceptanceError("final_master_acceptance_contract_id_mismatch")
    if int(acceptance.get("schema_version") or 0) != CONTRACT_SCHEMA_VERSION:
        raise FinalMasterAcceptanceError("final_master_acceptance_schema_mismatch")
    if acceptance.get("decision") != "pass":
        raise FinalMasterAcceptanceError("final_master_acceptance_decision_mismatch")

    from scripts import final_master_qc

    expected_policy = final_master_qc.qc_policy_fingerprint()
    if acceptance.get("qc_policy_fingerprint") != expected_policy:
        raise FinalMasterAcceptanceError("final_master_acceptance_policy_drift")
    if acceptance.get("implementation_sha256") != _implementation_sha256():
        raise FinalMasterAcceptanceError("final_master_acceptance_implementation_drift")

    stored_sources = acceptance.get("sources")
    if not isinstance(stored_sources, dict):
        raise FinalMasterAcceptanceError("final_master_acceptance_missing_sources")
    current_sources = _source_bindings(root)
    if stored_sources != current_sources:
        raise FinalMasterAcceptanceError("final_master_acceptance_artifact_identity_mismatch")

    upstream = acceptance.get("upstream_evidence")
    if not isinstance(upstream, dict):
        raise FinalMasterAcceptanceError("final_master_acceptance_invalid_upstream_evidence")
    for key, binding in upstream.items():
        if not isinstance(binding, dict):
            raise FinalMasterAcceptanceError("final_master_acceptance_invalid_upstream_binding")
        filename = str(binding.get("file") or "")
        if not filename or _regular_file_binding(root / filename) != binding:
            raise FinalMasterAcceptanceError(
                f"final_master_acceptance_upstream_identity_mismatch:{key}"
            )
    return document


def final_master_acceptance_sha256(output_dir: Path) -> str:
    document = require_final_master_acceptance(Path(output_dir))
    acceptance = document["acceptance_contract"]
    return str(acceptance["sources"]["final"]["sha256"])

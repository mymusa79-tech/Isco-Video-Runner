from __future__ import annotations

import hashlib
import json
from functools import wraps
from pathlib import Path
from typing import Any

from scripts.audio_producer_repair_lifecycle import REPORT_FILENAME, SCHEMA_VERSION
from scripts.audio_production_contract_v2 import require_audio_production_contract_v2


class AudioProducerCertificateError(RuntimeError):
    pass


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise AudioProducerCertificateError(f"audio_producer_certificate_invalid_json:{path.name}") from exc
    if not isinstance(value, dict):
        raise AudioProducerCertificateError(f"audio_producer_certificate_wrong_shape:{path.name}")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_audio_producer_certificate(output_dir: Path) -> dict[str, Any]:
    """Prove the accepted audio pre-gate receipt belongs to current final.mp4 bytes."""
    root = Path(output_dir)
    final_path = root / "final.mp4"
    if not final_path.is_file() or final_path.stat().st_size <= 1024:
        raise AudioProducerCertificateError("audio_producer_certificate_final_missing")
    plan = _read_json(root / "plan.json")
    quality = _read_json(root / "quality-final.json")
    report = _read_json(root / REPORT_FILENAME)
    if int(report.get("schema_version") or 0) != SCHEMA_VERSION:
        raise AudioProducerCertificateError("audio_producer_certificate_schema_mismatch")
    receipts = [item for item in list(report.get("receipts") or []) if isinstance(item, dict)]
    by_phase = {str(item.get("phase") or ""): item for item in receipts}

    fmt = str(plan.get("format") or quality.get("format") or "").strip().lower()
    short_finished = (root / "short-intelligence-pre-gold.json").is_file()
    required_phase = "short_finished" if fmt == "moment" and short_finished else "core_mux"
    receipt = by_phase.get(required_phase)
    if not isinstance(receipt, dict):
        raise AudioProducerCertificateError(
            f"audio_producer_certificate_missing_phase:{required_phase}"
        )

    receipt_sha = str(receipt.get("final_sha256") or "").strip().lower()
    current_sha = _sha256_file(final_path)
    if len(receipt_sha) != 64 or receipt_sha != current_sha:
        raise AudioProducerCertificateError(
            "audio_producer_certificate_final_identity_mismatch"
        )

    decision = str(receipt.get("decision") or "").strip()
    attempts = int(receipt.get("repair_attempts") or 0)
    if attempts not in {0, 1}:
        raise AudioProducerCertificateError("audio_producer_certificate_unbounded_repair_attempts")
    if decision == "pass" and attempts != 0:
        raise AudioProducerCertificateError("audio_producer_certificate_pass_attempt_mismatch")
    if decision == "repaired_pass" and attempts != 1:
        raise AudioProducerCertificateError("audio_producer_certificate_repair_attempt_mismatch")

    if decision == "not_applicable":
        if not (fmt == "moment" and not short_finished and int(quality.get("audio_streams") or 0) == 0):
            raise AudioProducerCertificateError("audio_producer_certificate_illegal_not_applicable")
    elif decision not in {"pass", "repaired_pass"}:
        raise AudioProducerCertificateError(
            f"audio_producer_certificate_not_accepted:{decision or 'missing'}"
        )

    print(
        "Audio Producer certificate PASS: "
        f"phase={required_phase} decision={decision} repair_attempts={attempts} final_sha256={current_sha[:12]}"
    )
    return receipt


def install_audio_producer_final_certificate(production_modules: list[Any]) -> None:
    """Place exact-byte Producer evidence and Audio Production V2 outside final gates."""
    installed = 0
    already_installed = 0
    for production in production_modules:
        current = getattr(production, "run_final_master_qc", None)
        if not callable(current):
            continue
        if getattr(current, "_isco_audio_producer_final_certificate", False):
            already_installed += 1
            continue

        def make_wrapper(original):
            @wraps(original)
            def wrapped(output_dir: Path, *args, **kwargs):
                # Order is deliberate: first prove the bounded producer repair receipt
                # belongs to the exact current bytes, then verify spoken semantic fidelity
                # on those same bytes, then hand them unchanged to the existing independent
                # Audio Semantic Integrity / Final Master QC chain.
                require_audio_producer_certificate(Path(output_dir))
                require_audio_production_contract_v2(Path(output_dir))
                return original(output_dir, *args, **kwargs)

            wrapped._isco_audio_producer_final_certificate = True
            wrapped._isco_audio_producer_final_certificate_original = original
            return wrapped

        production.run_final_master_qc = make_wrapper(current)
        installed += 1
    if installed <= 0 and already_installed <= 0:
        raise AudioProducerCertificateError("audio_producer_final_qc_binding_missing")

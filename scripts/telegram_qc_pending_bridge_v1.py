from __future__ import annotations

"""Production-V4 bridge for exact Gold QC_PENDING recovery carried by diagnostics.

The source Production workflow already uploads ``isco-resilient-v4-diagnostics-N`` on
failure.  A valid Gold checkpoint now carries a self-contained resume bundle inside that
artifact.  This module records that exact artifact in the Telegram ledger without
weakening the canonical generic-failure transition.
"""

from typing import Any

from scripts.telegram_production_queue import (
    QC_PENDING_FAILURE_REASON,
    _engine_sha,
    _now,
    _positive_run_id,
    _queue,
    _runner_sha,
    _sha256,
)

_DIAGNOSTIC_PREFIX = "isco-resilient-v4-diagnostics-"


def is_supported_qc_pending_artifact(value: object) -> bool:
    name = str(value or "").strip()
    if not name.startswith(_DIAGNOSTIC_PREFIX) or len(name) > 160:
        return False
    suffix = name.removeprefix(_DIAGNOSTIC_PREFIX)
    return suffix.isdigit() and int(suffix) > 0


def mark_dispatch_qc_pending_diagnostics(
    state: dict[str, Any],
    request_id: str,
    request_sha256: str,
    authorization_id: str,
    *,
    source_run_id: str,
    source_run_attempt: str,
    artifact_name: str,
    runner_sha: str,
    engine_sha: str,
    final_sha256: str,
    fmt: str,
) -> dict[str, Any]:
    source_run_id = _positive_run_id(source_run_id, label="QC_PENDING source run id")
    source_run_attempt = _positive_run_id(source_run_attempt, label="QC_PENDING source run attempt")
    runner_sha = _runner_sha(runner_sha)
    engine_sha = _engine_sha(engine_sha)
    final_sha256 = _sha256(final_sha256, label="QC_PENDING final hash")
    artifact_name = str(artifact_name or "").strip()
    if not is_supported_qc_pending_artifact(artifact_name):
        raise RuntimeError("QC_PENDING diagnostic artifact identity is invalid")
    fmt = str(fmt or "").strip().lower()
    if fmt not in {"film", "story", "moment"}:
        raise RuntimeError("QC_PENDING format is unsupported")

    expected = {
        "source_run_id": source_run_id,
        "source_run_attempt": source_run_attempt,
        "artifact_name": artifact_name,
        "runner_sha": runner_sha,
        "engine_sha": engine_sha,
        "final_sha256": final_sha256,
        "format": fmt,
    }
    for item in _queue(state):
        if not isinstance(item, dict):
            continue
        if (
            item.get("request_id") == request_id
            and item.get("request_sha256") == request_sha256
            and item.get("authorization_id") == str(authorization_id or "").strip()
        ):
            if item.get("status") == "qc_pending":
                if item.get("qc_pending") != expected:
                    raise RuntimeError("QC_PENDING identity changed for an existing ledger item")
                return item
            if item.get("status") != "dispatch_consumed":
                raise RuntimeError("Telegram dispatch is not consumed and cannot become QC_PENDING")
            pending_at = _now()
            item["status"] = "qc_pending"
            item["qc_pending_at"] = pending_at
            item["failure_reason"] = QC_PENDING_FAILURE_REASON
            item["qc_pending"] = expected
            state["last_event_at"] = pending_at
            return item
    raise RuntimeError("Exact Telegram dispatch authorization was not found for QC_PENDING transition")

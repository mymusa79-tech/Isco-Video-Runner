from __future__ import annotations

"""Bridge a validated Gold QC_PENDING checkpoint into the Telegram control plane.

This module is intentionally dependency-light and fail-closed. It does not decide that
an arbitrary production failure is resumable: the producer must already have written the
strict V2 Gold checkpoint. The bridge validates that checkpoint, builds the exact-byte
resume bundle, and returns the identity needed by ``telegram_v4_ingress qc-pending``.
"""

import argparse
import json
import os
from pathlib import Path
from typing import Any

from scripts.qc_pending_checkpoint_v1 import FILENAME, verify_qc_pending_checkpoint
from scripts.qc_pending_resume_bundle_v1 import build_resume_bundle, validate_resume_bundle


def _latest_output_with_checkpoint(engine_output: Path) -> Path | None:
    root = Path(engine_output)
    candidates = [
        path
        for path in root.glob("*")
        if path.is_dir() and (path / FILENAME).is_file()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def prepare_qc_pending_reconciliation(
    *,
    engine_output: Path,
    bundle_root: Path,
    source_run_id: str,
    source_run_attempt: str,
    runner_sha: str,
    engine_sha: str,
    run_number: str,
) -> dict[str, Any] | None:
    output_dir = _latest_output_with_checkpoint(Path(engine_output))
    if output_dir is None:
        return None

    checkpoint = verify_qc_pending_checkpoint(output_dir)
    expected_runner = str(runner_sha or "").strip().lower()
    expected_engine = str(engine_sha or "").strip().lower()
    expected_run = str(source_run_id or "").strip()
    expected_attempt = str(source_run_attempt or "").strip()

    if str(checkpoint.get("runner_sha") or "").strip().lower() != expected_runner:
        raise RuntimeError("QC_PENDING current Runner SHA mismatch")
    if str(checkpoint.get("engine_sha") or "").strip().lower() != expected_engine:
        raise RuntimeError("QC_PENDING current Engine SHA mismatch")
    if str(checkpoint.get("source_run_id") or "").strip() != expected_run:
        raise RuntimeError("QC_PENDING current workflow run mismatch")
    if str(checkpoint.get("source_run_attempt") or "").strip() != expected_attempt:
        raise RuntimeError("QC_PENDING current workflow attempt mismatch")

    artifact_name = f"isco-qc-pending-{str(run_number or expected_run).strip()}"
    destination = Path(bundle_root) / artifact_name
    manifest = build_resume_bundle(output_dir, destination)
    validate_resume_bundle(
        destination,
        expected_source_run_id=expected_run,
        expected_runner_sha=expected_runner,
        expected_engine_sha=expected_engine,
    )
    final_sha = str(manifest.get("final_sha256") or "").strip().lower()
    if final_sha != str((checkpoint.get("final") or {}).get("sha256") or "").strip().lower():
        raise RuntimeError("QC_PENDING bundle/checkpoint final identity mismatch")

    return {
        "status": "qc_pending",
        "checkpoint": str((output_dir / FILENAME).resolve()),
        "output_dir": str(output_dir.resolve()),
        "bundle_dir": str(destination.resolve()),
        "artifact_name": artifact_name,
        "final_sha256": final_sha,
        "format": str(checkpoint.get("format") or "").strip().lower(),
        "source_run_id": expected_run,
        "source_run_attempt": expected_attempt,
        "runner_sha": expected_runner,
        "engine_sha": expected_engine,
    }


def _write_github_output(path: Path | None, values: dict[str, Any]) -> None:
    if path is None:
        return
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"qc_pending={'true' if values else 'false'}\n")
        for key in (
            "checkpoint", "output_dir", "bundle_dir", "artifact_name", "final_sha256",
            "format", "source_run_id", "source_run_attempt", "runner_sha", "engine_sha",
        ):
            handle.write(f"{key}={values.get(key, '') if values else ''}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine-output", type=Path, default=Path("engine/output"))
    parser.add_argument("--bundle-root", type=Path, required=True)
    parser.add_argument("--source-run-id", default=os.environ.get("GITHUB_RUN_ID", ""))
    parser.add_argument("--source-run-attempt", default=os.environ.get("GITHUB_RUN_ATTEMPT", ""))
    parser.add_argument("--runner-sha", default=os.environ.get("GITHUB_SHA", ""))
    parser.add_argument("--engine-sha", default=os.environ.get("ISCO_ENGINE_SHA", ""))
    parser.add_argument("--run-number", default=os.environ.get("GITHUB_RUN_NUMBER", ""))
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()

    result = prepare_qc_pending_reconciliation(
        engine_output=args.engine_output,
        bundle_root=args.bundle_root,
        source_run_id=args.source_run_id,
        source_run_attempt=args.source_run_attempt,
        runner_sha=args.runner_sha,
        engine_sha=args.engine_sha,
        run_number=args.run_number,
    )
    values = result or {}
    _write_github_output(args.github_output, values)
    print(json.dumps(values or {"status": "not_qc_pending"}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

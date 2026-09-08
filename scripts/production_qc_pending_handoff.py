from __future__ import annotations

"""Bridge an exact production QC_PENDING checkpoint into a durable Gold-resume artifact.

This module is intentionally narrow. It does not infer recoverability from logs or error
strings. The Engine/Runner Gold checkpoint remains the sole authority. If there is no
checkpoint, the caller continues with the ordinary failure path. If a checkpoint exists
but cannot be verified or bundled, this bridge fails closed and no resumable Telegram
state should be persisted.
"""

import argparse
import os
from pathlib import Path
from typing import Any

from scripts.qc_pending_checkpoint_v1 import verify_qc_pending_checkpoint
from scripts.qc_pending_resume_bundle_v1 import build_resume_bundle


def _github_output(path: Path | None, values: dict[str, object]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def _positive(value: object, *, label: str) -> str:
    text = str(value or "").strip()
    if not text.isdigit() or int(text) < 1:
        raise RuntimeError(f"{label} must be a positive integer")
    return text


def prepare_qc_pending_handoff(
    *,
    engine_output_root: Path,
    destination: Path,
    run_number: str,
    run_attempt: str,
    current_run_id: str,
    current_runner_sha: str,
    github_output: Path | None = None,
) -> dict[str, Any]:
    root = Path(engine_output_root).resolve()
    checkpoints = sorted(root.glob("*/qc-pending.json")) if root.is_dir() else []
    if not checkpoints:
        values: dict[str, Any] = {"eligible": "false"}
        _github_output(github_output, values)
        return values
    if len(checkpoints) != 1:
        raise RuntimeError("QC_PENDING handoff requires exactly one checkpointed output")

    checkpoint_path = checkpoints[0]
    output_dir = checkpoint_path.parent
    checkpoint = verify_qc_pending_checkpoint(output_dir)

    run_id = _positive(current_run_id, label="current GitHub run id")
    run_attempt_value = _positive(run_attempt, label="current GitHub run attempt")
    run_number_value = _positive(run_number, label="current GitHub run number")
    source_run_id = str(checkpoint.get("source_run_id") or "").strip()
    source_run_attempt = str(checkpoint.get("source_run_attempt") or "").strip()
    runner_sha = str(checkpoint.get("runner_sha") or "").strip().lower()
    expected_runner_sha = str(current_runner_sha or "").strip().lower()
    if source_run_id != run_id or source_run_attempt != run_attempt_value:
        raise RuntimeError("QC_PENDING source run identity does not match current production run")
    if runner_sha != expected_runner_sha:
        raise RuntimeError("QC_PENDING Runner SHA does not match current production checkout")

    bundle_dir = Path(destination).resolve()
    manifest = build_resume_bundle(output_dir, bundle_dir)
    artifact_name = f"isco-qc-pending-{run_number_value}-{run_attempt_value}"
    values = {
        "eligible": "true",
        "artifact_name": artifact_name,
        "checkpoint_path": str(checkpoint_path.resolve()),
        "output_dir": str(output_dir.resolve()),
        "bundle_dir": str(bundle_dir),
        "final_sha256": str(manifest.get("final_sha256") or ""),
        "format": str(manifest.get("format") or ""),
    }
    _github_output(github_output, values)
    return values


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine-output-root", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--run-number", default=os.environ.get("GITHUB_RUN_NUMBER", ""))
    parser.add_argument("--run-attempt", default=os.environ.get("GITHUB_RUN_ATTEMPT", ""))
    parser.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID", ""))
    parser.add_argument("--runner-sha", default=os.environ.get("GITHUB_SHA", ""))
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    values = prepare_qc_pending_handoff(
        engine_output_root=args.engine_output_root,
        destination=args.destination,
        run_number=args.run_number,
        run_attempt=args.run_attempt,
        current_run_id=args.run_id,
        current_runner_sha=args.runner_sha,
        github_output=args.github_output,
    )
    print("QC_PENDING production handoff:", values)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

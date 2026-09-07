from __future__ import annotations

"""Persist and verify the minimal exact-byte bundle needed for Gold-only resume."""

import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any

from scripts.qc_pending_checkpoint_v1 import verify_qc_pending_checkpoint


CONTRACT_ID = "gold.qc-pending.resume-bundle.v1"
CONTRACT_VERSION = 1
MANIFEST_FILENAME = "resume-manifest.json"
REQUIRED_FILES = (
    "final.mp4",
    "plan.json",
    "quality-final.json",
    "visual-audit.json",
    "rights-manifest.json",
    "monetization-check.json",
    "opening-visual-audit.json",
    "final-master-qc.json",
    "ai-budget.json",
    "qc-pending.json",
)
OPTIONAL_FILES = (
    "short-visual-timeline.json",
    "production-failure-diagnostics.json",
)
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_RUN_ID = re.compile(r"^[1-9][0-9]*$")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"Resume bundle JSON object required: {path.name}")
    return value


def _identity(checkpoint: dict[str, Any]) -> tuple[str, str, str, str]:
    runner_sha = str(checkpoint.get("runner_sha") or "").strip().lower()
    engine_sha = str(checkpoint.get("engine_sha") or "").strip().lower()
    source_run_id = str(checkpoint.get("source_run_id") or "").strip()
    source_run_attempt = str(checkpoint.get("source_run_attempt") or "").strip()
    if _SHA40.fullmatch(runner_sha) is None:
        raise RuntimeError("Resume bundle requires exact 40-hex Runner SHA")
    if _SHA40.fullmatch(engine_sha) is None:
        raise RuntimeError("Resume bundle requires exact 40-hex Engine SHA")
    if _RUN_ID.fullmatch(source_run_id) is None:
        raise RuntimeError("Resume bundle requires source GitHub run id")
    if _RUN_ID.fullmatch(source_run_attempt or "") is None:
        raise RuntimeError("Resume bundle requires source GitHub run attempt")
    return runner_sha, engine_sha, source_run_id, source_run_attempt


def build_resume_bundle(output_dir: Path, destination: Path) -> dict[str, Any]:
    source = Path(output_dir).resolve()
    destination = Path(destination).resolve()
    checkpoint = verify_qc_pending_checkpoint(source)
    runner_sha, engine_sha, source_run_id, source_run_attempt = _identity(checkpoint)

    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=False)

    copied: dict[str, dict[str, Any]] = {}
    for name in REQUIRED_FILES:
        path = source / name
        if not path.is_file():
            raise RuntimeError(f"QC_PENDING resume bundle missing required file: {name}")
        target = destination / name
        shutil.copy2(path, target)
        copied[name] = {
            "sha256": _sha256_file(target),
            "byte_length": target.stat().st_size,
            "required": True,
        }
    for name in OPTIONAL_FILES:
        path = source / name
        if not path.is_file():
            continue
        target = destination / name
        shutil.copy2(path, target)
        copied[name] = {
            "sha256": _sha256_file(target),
            "byte_length": target.stat().st_size,
            "required": False,
        }

    final_sha = str((checkpoint.get("final") or {}).get("sha256") or "")
    if copied["final.mp4"]["sha256"] != final_sha:
        raise RuntimeError("Resume bundle final.mp4 does not match QC_PENDING checkpoint")
    if copied["qc-pending.json"]["sha256"] != _sha256_file(source / "qc-pending.json"):
        raise RuntimeError("Resume bundle checkpoint changed while copying")

    manifest = {
        "schema_version": CONTRACT_VERSION,
        "contract_id": CONTRACT_ID,
        "release_allowed": False,
        "resume_scope": "gold_enforcement_only_no_replan_no_retrieval_no_rerender_no_tts",
        "source": {
            "runner_sha": runner_sha,
            "engine_sha": engine_sha,
            "run_id": source_run_id,
            "run_attempt": source_run_attempt,
            "output_directory_name": source.name,
        },
        "final_sha256": final_sha,
        "checkpoint_sha256": copied["qc-pending.json"]["sha256"],
        "files": copied,
    }
    (destination / MANIFEST_FILENAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    validate_resume_bundle(destination)
    return manifest


def validate_resume_bundle(
    bundle_dir: Path,
    *,
    expected_source_run_id: str | None = None,
    expected_runner_sha: str | None = None,
    expected_engine_sha: str | None = None,
) -> dict[str, Any]:
    root = Path(bundle_dir).resolve()
    manifest = _read_object(root / MANIFEST_FILENAME)
    if manifest.get("contract_id") != CONTRACT_ID or manifest.get("schema_version") != CONTRACT_VERSION:
        raise RuntimeError("QC_PENDING resume manifest contract mismatch")
    if manifest.get("release_allowed") is not False:
        raise RuntimeError("QC_PENDING resume manifest cannot authorize release")

    source = manifest.get("source") or {}
    runner_sha = str(source.get("runner_sha") or "").strip().lower()
    engine_sha = str(source.get("engine_sha") or "").strip().lower()
    run_id = str(source.get("run_id") or "").strip()
    if expected_source_run_id is not None and run_id != str(expected_source_run_id).strip():
        raise RuntimeError("QC_PENDING resume source run id mismatch")
    if expected_runner_sha is not None and runner_sha != str(expected_runner_sha).strip().lower():
        raise RuntimeError("QC_PENDING resume Runner SHA mismatch")
    if expected_engine_sha is not None and engine_sha != str(expected_engine_sha).strip().lower():
        raise RuntimeError("QC_PENDING resume Engine SHA mismatch")

    files = manifest.get("files")
    if not isinstance(files, dict):
        raise RuntimeError("QC_PENDING resume manifest file map missing")
    for name in REQUIRED_FILES:
        if name not in files:
            raise RuntimeError(f"QC_PENDING resume manifest omitted required file: {name}")
    for name, evidence in files.items():
        if name not in REQUIRED_FILES and name not in OPTIONAL_FILES:
            raise RuntimeError(f"QC_PENDING resume manifest contains unexpected file: {name}")
        if not isinstance(evidence, dict):
            raise RuntimeError(f"QC_PENDING resume manifest evidence malformed: {name}")
        path = root / name
        if not path.is_file():
            raise RuntimeError(f"QC_PENDING resume bundle file missing: {name}")
        if _sha256_file(path) != str(evidence.get("sha256") or ""):
            raise RuntimeError(f"QC_PENDING resume bundle hash mismatch: {name}")
        if path.stat().st_size != int(evidence.get("byte_length") or -1):
            raise RuntimeError(f"QC_PENDING resume bundle size mismatch: {name}")

    checkpoint = verify_qc_pending_checkpoint(root)
    if str((checkpoint.get("final") or {}).get("sha256") or "") != str(manifest.get("final_sha256") or ""):
        raise RuntimeError("QC_PENDING resume checkpoint/final identity mismatch")
    if _sha256_file(root / "qc-pending.json") != str(manifest.get("checkpoint_sha256") or ""):
        raise RuntimeError("QC_PENDING resume checkpoint hash mismatch")
    cp_runner, cp_engine, cp_run_id, cp_attempt = _identity(checkpoint)
    if (cp_runner, cp_engine, cp_run_id, cp_attempt) != (
        runner_sha,
        engine_sha,
        run_id,
        str(source.get("run_attempt") or "").strip(),
    ):
        raise RuntimeError("QC_PENDING resume manifest/checkpoint provenance mismatch")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--output-dir", required=True, type=Path)
    build.add_argument("--destination", required=True, type=Path)
    verify = sub.add_parser("verify")
    verify.add_argument("--bundle", required=True, type=Path)
    verify.add_argument("--source-run-id")
    verify.add_argument("--runner-sha")
    verify.add_argument("--engine-sha")
    args = parser.parse_args()
    if args.command == "build":
        manifest = build_resume_bundle(args.output_dir, args.destination)
    else:
        manifest = validate_resume_bundle(
            args.bundle,
            expected_source_run_id=args.source_run_id,
            expected_runner_sha=args.runner_sha,
            expected_engine_sha=args.engine_sha,
        )
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
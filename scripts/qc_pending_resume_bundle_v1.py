from __future__ import annotations

"""Persist and verify the exact-byte evidence needed to resume from Gold onward.

The bundle never authorizes release by itself. It preserves the already-rendered media,
pre-Gold quality evidence and the minimum post-Gold inputs needed to finish the *same
approved scope* without replanning, researching, retrieving media, resynthesizing the
parent voice, or rerendering the parent final.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any


CONTRACT_ID = "gold.qc-pending.resume-bundle.v2"
CONTRACT_VERSION = 2
MANIFEST_FILENAME = "resume-manifest.json"
COMMON_REQUIRED_FILES = (
    "final.mp4",
    "plan.json",
    "quality-final.json",
    "visual-audit.json",
    "rights-manifest.json",
    "monetization-check.json",
    "factuality-audit.json",
    "content-quality-audit.json",
    "tone-quality-audit.json",
    "final-master-qc.json",
    "ai-budget.json",
    "qc-pending.json",
)
SHORT_REQUIRED_FILES = (
    "short-intelligence-pre-gold.json",
)
OPTIONAL_FILES = (
    # Gold Vision can fail before this file is written (the exact Run #230 shape).
    # It is post-checkpoint work that Gold-only resume re-executes, not pre-Gold
    # evidence required to preserve the exact rendered media.
    "opening-visual-audit.json",
    "short-visual-timeline.json",
    "short-retention-contract.json",
    "short-compensation-plan.json",
    "short-progressive.srt",
    "audio-mastering.json",
    "voice-identity-audit.json",
    "production-failure-diagnostics.json",
    "planning-telemetry.json",
    # Chained Audio-QC -> Gold recovery evidence. These remain optional unless the
    # exact Final Master receipt binds them as upstream evidence. Bound receipt files
    # are discovered dynamically below and become required for that exact bundle.
    "audio-qc-pending.json",
    "audio-production-contract-v2.json",
    "audio-semantic-resume-state.json",
    "audio-resume-budget-envelope.json",
    "ai-budget-audio-resume.json",
)
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_RUN_ID = re.compile(r"^[1-9][0-9]*$")


def _verify_qc_pending_checkpoint(path: Path) -> dict[str, Any]:
    """Load Engine-dependent checkpoint verification only at the recovery boundary.

    Telegram/Edge UI imports this module in lightweight jobs that intentionally do not
    install the private Engine. Keeping the import lazy preserves that hermeticity while
    production and Gold-resume runtimes still execute the exact authoritative verifier.
    """
    from scripts.qc_pending_checkpoint_v1 import verify_qc_pending_checkpoint

    return verify_qc_pending_checkpoint(path)


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


def _format(checkpoint: dict[str, Any]) -> str:
    fmt = str(checkpoint.get("format") or "").strip().lower()
    if fmt not in {"film", "story", "moment"}:
        raise RuntimeError("Resume bundle checkpoint format is unsupported")
    return fmt


def _required_files(checkpoint: dict[str, Any]) -> tuple[str, ...]:
    fmt = _format(checkpoint)
    return COMMON_REQUIRED_FILES + (SHORT_REQUIRED_FILES if fmt == "moment" else ())


def _safe_receipt_filename(value: object, *, evidence_key: object) -> str:
    """Return one safe bundle-local filename from Final Master evidence.

    Final Master currently emits basename-only bindings. Keep that ownership invariant
    explicit here so a future malformed receipt cannot turn recovery copying into path
    traversal or authorize arbitrary files outside the production output directory.
    """
    name = str(value or "").strip()
    if (
        not name
        or name in {".", "..", MANIFEST_FILENAME}
        or "/" in name
        or "\\" in name
        or Path(name).is_absolute()
        or Path(name).name != name
    ):
        raise RuntimeError(
            "QC_PENDING Final Master upstream evidence has unsafe file binding: "
            f"{str(evidence_key or 'unknown')}"
        )
    return name


def _final_master_upstream_evidence_files(root: Path) -> tuple[str, ...]:
    """Discover the exact dynamic dependency closure owned by Final Master.

    Run #241 exposed why this cannot be a second hand-maintained allow-list: F24 bound
    ``audio-producer-repair.json`` in ``upstream_evidence`` but the resume bundle copied
    only ``audio-production-contract-v2.json``. The receipt is authoritative, so every
    file it binds is required by recovery regardless of long/short format or which
    upstream producer introduced the evidence.
    """
    report = _read_object(Path(root) / "final-master-qc.json")
    acceptance = report.get("acceptance_contract")
    if not isinstance(acceptance, dict):
        raise RuntimeError("QC_PENDING Final Master acceptance contract missing")
    upstream = acceptance.get("upstream_evidence")
    if not isinstance(upstream, dict):
        raise RuntimeError("QC_PENDING Final Master upstream evidence map missing")

    names: list[str] = []
    for evidence_key, binding in upstream.items():
        if not isinstance(binding, dict):
            raise RuntimeError(
                "QC_PENDING Final Master upstream evidence binding malformed: "
                f"{str(evidence_key or 'unknown')}"
            )
        name = _safe_receipt_filename(binding.get("file"), evidence_key=evidence_key)
        if name not in names:
            names.append(name)
    return tuple(names)


def _ordered_unique(*groups: tuple[str, ...]) -> tuple[str, ...]:
    result: list[str] = []
    for group in groups:
        for name in group:
            if name not in result:
                result.append(name)
    return tuple(result)


def _copy_bundle_file(
    *,
    source: Path,
    destination: Path,
    name: str,
    required: bool,
) -> dict[str, Any] | None:
    path = source / name
    if not path.is_file() or path.is_symlink():
        if required:
            raise RuntimeError(f"QC_PENDING resume bundle missing required file: {name}")
        return None
    target = destination / name
    shutil.copy2(path, target)
    return {
        "sha256": _sha256_file(target),
        "byte_length": target.stat().st_size,
        "required": required,
    }


def build_resume_bundle(output_dir: Path, destination: Path) -> dict[str, Any]:
    source = Path(output_dir).resolve()
    destination = Path(destination).resolve()
    checkpoint = _verify_qc_pending_checkpoint(source)
    runner_sha, engine_sha, source_run_id, source_run_attempt = _identity(checkpoint)
    fmt = _format(checkpoint)
    receipt_files = _final_master_upstream_evidence_files(source)
    required_files = _ordered_unique(_required_files(checkpoint), receipt_files)

    # Build out of sight and publish only after full self-validation. Run #241 left a
    # partial destination directory because validation happened after writing directly
    # into the final namespace. Staging keeps a failed attempt non-discoverable as a
    # resumable bundle and preserves any previously valid destination until replacement.
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.staging-",
            dir=str(destination.parent),
        )
    ).resolve()

    try:
        copied: dict[str, dict[str, Any]] = {}
        for name in required_files:
            evidence = _copy_bundle_file(
                source=source,
                destination=staging,
                name=name,
                required=True,
            )
            assert evidence is not None
            copied[name] = evidence

        for name in OPTIONAL_FILES:
            if name in copied:
                continue
            evidence = _copy_bundle_file(
                source=source,
                destination=staging,
                name=name,
                required=False,
            )
            if evidence is not None:
                copied[name] = evidence

        final_sha = str((checkpoint.get("final") or {}).get("sha256") or "")
        if copied["final.mp4"]["sha256"] != final_sha:
            raise RuntimeError("Resume bundle final.mp4 does not match QC_PENDING checkpoint")
        if copied["qc-pending.json"]["sha256"] != _sha256_file(source / "qc-pending.json"):
            raise RuntimeError("Resume bundle checkpoint changed while copying")

        manifest = {
            "schema_version": CONTRACT_VERSION,
            "contract_id": CONTRACT_ID,
            "release_allowed": False,
            "resume_scope": "from_gold_to_same_approved_delivery_no_parent_rebuild",
            "source": {
                "runner_sha": runner_sha,
                "engine_sha": engine_sha,
                "run_id": source_run_id,
                "run_attempt": source_run_attempt,
                "output_directory_name": source.name,
            },
            "format": fmt,
            "parent_media_rebuild_allowed": False,
            "post_gold_approved_scope_continuation_allowed": True,
            "final_sha256": final_sha,
            "checkpoint_sha256": copied["qc-pending.json"]["sha256"],
            "final_master_upstream_evidence_files": list(receipt_files),
            "files": copied,
        }
        (staging / MANIFEST_FILENAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

        # Validate the exact staged bytes before they can acquire the stable recovery
        # directory name consumed by diagnostics/Gold-resume discovery.
        validate_resume_bundle(staging)

        if destination.exists():
            if destination.is_symlink() or not destination.is_dir():
                raise RuntimeError("QC_PENDING resume destination is not a replaceable directory")
            shutil.rmtree(destination)
        os.replace(staging, destination)
        return manifest
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


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
    if manifest.get("parent_media_rebuild_allowed") is not False:
        raise RuntimeError("QC_PENDING resume manifest attempted to authorize parent media rebuild")

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

    checkpoint = _verify_qc_pending_checkpoint(root)
    receipt_files = _final_master_upstream_evidence_files(root)
    required_files = _ordered_unique(_required_files(checkpoint), receipt_files)
    if str(manifest.get("format") or "") != _format(checkpoint):
        raise RuntimeError("QC_PENDING resume manifest/checkpoint format mismatch")
    declared_receipt_files = manifest.get("final_master_upstream_evidence_files")
    if declared_receipt_files != list(receipt_files):
        raise RuntimeError("QC_PENDING resume manifest Final Master evidence closure mismatch")

    files = manifest.get("files")
    if not isinstance(files, dict):
        raise RuntimeError("QC_PENDING resume manifest file map missing")
    for name in required_files:
        if name not in files:
            raise RuntimeError(f"QC_PENDING resume manifest omitted required file: {name}")
        evidence = files.get(name)
        if not isinstance(evidence, dict) or evidence.get("required") is not True:
            raise RuntimeError(f"QC_PENDING resume manifest did not require bound file: {name}")

    allowed = set(COMMON_REQUIRED_FILES) | set(SHORT_REQUIRED_FILES) | set(OPTIONAL_FILES) | set(receipt_files)
    for name, evidence in files.items():
        if name not in allowed:
            raise RuntimeError(f"QC_PENDING resume manifest contains unexpected file: {name}")
        if not isinstance(evidence, dict):
            raise RuntimeError(f"QC_PENDING resume manifest evidence malformed: {name}")
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"QC_PENDING resume bundle file missing: {name}")
        if _sha256_file(path) != str(evidence.get("sha256") or ""):
            raise RuntimeError(f"QC_PENDING resume bundle hash mismatch: {name}")
        if path.stat().st_size != int(evidence.get("byte_length") or -1):
            raise RuntimeError(f"QC_PENDING resume bundle size mismatch: {name}")

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

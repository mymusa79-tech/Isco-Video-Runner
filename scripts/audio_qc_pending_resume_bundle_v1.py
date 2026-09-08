from __future__ import annotations

"""Build/verify the minimal durable bundle for an AUDIO_QC_PENDING production.

The bundle intentionally keeps the exact parent final.mp4 plus deterministic JSON
release evidence.  It does not retain stock source clips, picture intermediates or any
other media that could tempt a resume path to rerender the parent.  Long-form retains
only the exact TTS section WAVs/narration referenced by the persisted Audio Semantic
Integrity state because those bytes are needed to re-prove provenance in a new process.
"""

import argparse
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from scripts.audio_qc_pending_checkpoint_v1 import CONTRACT_ID as CHECKPOINT_CONTRACT_ID
from scripts.audio_qc_pending_checkpoint_v1 import FILENAME as CHECKPOINT_FILENAME
from scripts.audio_qc_pending_checkpoint_v1 import STATUS as CHECKPOINT_STATUS
from scripts.audio_semantic_resume_state_v1 import CONTRACT_ID as SEMANTIC_CONTRACT_ID
from scripts.audio_semantic_resume_state_v1 import FILENAME as SEMANTIC_FILENAME


CONTRACT_ID = "audio.qc-pending.resume-bundle.v1"
SCHEMA_VERSION = 1
MANIFEST_FILENAME = "resume-manifest.json"

# Never carry stale post-Audio acceptance evidence into a new process.  The resume must
# recreate every one of these from the same final bytes after Audio Production V2 passes.
_FORBIDDEN_JSON = {
    "final-master-qc.json",
    "final-critic.json",
    "opening-visual-audit.json",
    "viewer-quality-contract.json",
    "gold-enforce-report.json",
    "gold-packaging-acceptance.json",
    "production-manifest.json",
    "delivery-manifest.json",
    "thumbnail-plan.json",
    "release-transaction.json",
}


class AudioQCPendingBundleError(RuntimeError):
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
        raise AudioQCPendingBundleError(f"audio_qc_bundle_invalid_json:{Path(path).name}") from exc
    if not isinstance(value, dict):
        raise AudioQCPendingBundleError(f"audio_qc_bundle_wrong_shape:{Path(path).name}")
    return value


def _safe_rel(value: object) -> Path:
    text = str(value or "").strip()
    path = Path(text)
    if not text or path.is_absolute() or ".." in path.parts or any(part in {"", "."} for part in path.parts):
        raise AudioQCPendingBundleError("audio_qc_bundle_unsafe_relative_path")
    return path


def _copy_file(source_root: Path, bundle_root: Path, relative: Path) -> dict[str, Any]:
    source = source_root / relative
    try:
        resolved = source.resolve(strict=True)
        resolved.relative_to(source_root.resolve())
    except (OSError, ValueError) as exc:
        raise AudioQCPendingBundleError(f"audio_qc_bundle_source_path_invalid:{relative.as_posix()}") from exc
    if source.is_symlink() or resolved.is_symlink() or not resolved.is_file():
        raise AudioQCPendingBundleError(f"audio_qc_bundle_source_file_invalid:{relative.as_posix()}")
    destination = bundle_root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(resolved, destination)
    return {
        "path": relative.as_posix(),
        "sha256": _sha256_file(destination),
        "bytes": destination.stat().st_size,
    }


def _semantic_media_files(root: Path, semantic: dict[str, Any]) -> list[Path]:
    fmt = str(semantic.get("format") or "").strip().lower()
    if fmt == "moment":
        return []
    paths: list[Path] = []
    items = semantic.get("tts_sections")
    if not isinstance(items, list) or not items:
        raise AudioQCPendingBundleError("audio_qc_bundle_long_tts_state_missing")
    for item in items:
        if not isinstance(item, dict):
            raise AudioQCPendingBundleError("audio_qc_bundle_long_tts_state_invalid")
        paths.append(_safe_rel(item.get("audio_path")))
    narration = semantic.get("narration")
    if not isinstance(narration, dict):
        raise AudioQCPendingBundleError("audio_qc_bundle_long_narration_state_missing")
    paths.append(_safe_rel(narration.get("path")))
    if len(paths) != len(set(paths)):
        raise AudioQCPendingBundleError("audio_qc_bundle_semantic_media_duplicate_path")
    return paths


def _validate_source_checkpoint(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    checkpoint = _read_object(root / CHECKPOINT_FILENAME)
    if checkpoint.get("contract_id") != CHECKPOINT_CONTRACT_ID or checkpoint.get("status") != CHECKPOINT_STATUS:
        raise AudioQCPendingBundleError("audio_qc_bundle_checkpoint_contract_mismatch")
    if checkpoint.get("resumable") is not True or checkpoint.get("release_allowed") is not False:
        raise AudioQCPendingBundleError("audio_qc_bundle_checkpoint_state_invalid")
    final = checkpoint.get("final")
    if not isinstance(final, dict):
        raise AudioQCPendingBundleError("audio_qc_bundle_checkpoint_final_missing")
    final_path = root / "final.mp4"
    if not final_path.is_file() or final_path.stat().st_size <= 1024:
        raise AudioQCPendingBundleError("audio_qc_bundle_final_missing")
    if _sha256_file(final_path) != str(final.get("sha256") or "").strip().lower():
        raise AudioQCPendingBundleError("audio_qc_bundle_final_hash_mismatch")
    if final_path.stat().st_size != int(final.get("bytes") or 0):
        raise AudioQCPendingBundleError("audio_qc_bundle_final_size_mismatch")

    semantic = _read_object(root / SEMANTIC_FILENAME)
    if semantic.get("contract_id") != SEMANTIC_CONTRACT_ID:
        raise AudioQCPendingBundleError("audio_qc_bundle_semantic_contract_mismatch")
    if str((semantic.get("final") or {}).get("sha256") or "").strip().lower() != str(final.get("sha256") or "").strip().lower():
        raise AudioQCPendingBundleError("audio_qc_bundle_semantic_final_mismatch")
    source = checkpoint.get("source")
    if not isinstance(source, dict) or semantic.get("production_id") != source.get("production_id"):
        raise AudioQCPendingBundleError("audio_qc_bundle_semantic_production_identity_mismatch")
    return checkpoint, semantic


def build_resume_bundle(output_dir: Path, bundle_dir: Path) -> dict[str, Any]:
    source_root = Path(output_dir).resolve()
    bundle_root = Path(bundle_dir).resolve()
    if bundle_root == source_root or source_root in bundle_root.parents:
        raise AudioQCPendingBundleError("audio_qc_bundle_destination_inside_source_forbidden")
    if bundle_root.exists():
        shutil.rmtree(bundle_root)
    bundle_root.mkdir(parents=True, exist_ok=False)

    checkpoint, semantic = _validate_source_checkpoint(source_root)
    source = checkpoint["source"]
    required_json = {
        "plan.json",
        "quality-final.json",
        "visual-audit.json",
        "rights-manifest.json",
        "monetization-check.json",
        "audio-producer-repair.json",
        "audio-retention-qc.json",
        "audio-production-contract-v2.json",
        SEMANTIC_FILENAME,
        CHECKPOINT_FILENAME,
        "ai-budget.json",
    }
    fmt = str(checkpoint.get("format") or "").strip().lower()
    if fmt in {"film", "story"}:
        required_json.add("audio-mastering.json")
    elif fmt == "moment":
        required_json.update({"short-intelligence-pre-gold.json", "short-visual-timeline.json"})
    else:
        raise AudioQCPendingBundleError("audio_qc_bundle_format_invalid")

    missing = sorted(name for name in required_json if not (source_root / name).is_file())
    if missing:
        raise AudioQCPendingBundleError("audio_qc_bundle_required_artifacts_missing:" + ",".join(missing))

    # Keep all source JSON evidence except post-Audio/post-Gold outputs that must be
    # re-created.  JSON is small, deterministic, and protects future format-specific
    # finalization from losing an already-paid-for quality artifact.
    json_names = {
        path.name
        for path in source_root.glob("*.json")
        if path.is_file() and path.name not in _FORBIDDEN_JSON
    }
    json_names.update(required_json)
    files: list[dict[str, Any]] = []
    files.append(_copy_file(source_root, bundle_root, Path("final.mp4")))
    for name in sorted(json_names):
        files.append(_copy_file(source_root, bundle_root, Path(name)))
    for relative in _semantic_media_files(source_root, semantic):
        files.append(_copy_file(source_root, bundle_root, relative))

    seen = [item["path"] for item in files]
    if len(seen) != len(set(seen)):
        raise AudioQCPendingBundleError("audio_qc_bundle_duplicate_manifest_path")

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "contract_id": CONTRACT_ID,
        "source": {
            "run_id": str(source.get("run_id") or ""),
            "run_number": str(source.get("run_number") or ""),
            "run_attempt": str(source.get("run_attempt") or ""),
            "runner_sha": str(source.get("runner_sha") or "").lower(),
            "engine_sha": str(source.get("engine_sha") or "").lower(),
            "production_id": str(source.get("production_id") or ""),
            "output_directory_name": source_root.name,
        },
        "format": fmt,
        "ingress": checkpoint.get("ingress"),
        "release_tag": checkpoint.get("release_tag"),
        "final_sha256": checkpoint["final"]["sha256"],
        "parent_media_rebuilt": False,
        "resume_scope": checkpoint["retry_policy"]["resume_scope"],
        "files": sorted(files, key=lambda item: item["path"]),
    }
    (bundle_root / MANIFEST_FILENAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    validate_resume_bundle(
        bundle_root,
        expected_source_run_id=str(source.get("run_id") or ""),
        expected_runner_sha=str(source.get("runner_sha") or ""),
        expected_engine_sha=str(source.get("engine_sha") or ""),
    )
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
    if manifest.get("contract_id") != CONTRACT_ID or int(manifest.get("schema_version") or 0) != SCHEMA_VERSION:
        raise AudioQCPendingBundleError("audio_qc_bundle_manifest_contract_mismatch")
    source = manifest.get("source")
    if not isinstance(source, dict):
        raise AudioQCPendingBundleError("audio_qc_bundle_source_identity_missing")
    if expected_source_run_id is not None and str(source.get("run_id") or "") != str(expected_source_run_id):
        raise AudioQCPendingBundleError("audio_qc_bundle_source_run_mismatch")
    if expected_runner_sha is not None and str(source.get("runner_sha") or "").lower() != str(expected_runner_sha).lower():
        raise AudioQCPendingBundleError("audio_qc_bundle_source_runner_mismatch")
    if expected_engine_sha is not None and str(source.get("engine_sha") or "").lower() != str(expected_engine_sha).lower():
        raise AudioQCPendingBundleError("audio_qc_bundle_source_engine_mismatch")

    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        raise AudioQCPendingBundleError("audio_qc_bundle_manifest_files_missing")
    paths: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise AudioQCPendingBundleError("audio_qc_bundle_manifest_file_invalid")
        relative = _safe_rel(entry.get("path"))
        text = relative.as_posix()
        if text in paths:
            raise AudioQCPendingBundleError("audio_qc_bundle_manifest_duplicate_path")
        paths.add(text)
        candidate = root / relative
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError) as exc:
            raise AudioQCPendingBundleError(f"audio_qc_bundle_file_path_escaped:{text}") from exc
        if candidate.is_symlink() or resolved.is_symlink() or not resolved.is_file():
            raise AudioQCPendingBundleError(f"audio_qc_bundle_file_invalid:{text}")
        if _sha256_file(resolved) != str(entry.get("sha256") or "").strip().lower():
            raise AudioQCPendingBundleError(f"audio_qc_bundle_file_hash_mismatch:{text}")
        if resolved.stat().st_size != int(entry.get("bytes") or -1):
            raise AudioQCPendingBundleError(f"audio_qc_bundle_file_size_mismatch:{text}")

    if "final.mp4" not in paths or CHECKPOINT_FILENAME not in paths or SEMANTIC_FILENAME not in paths:
        raise AudioQCPendingBundleError("audio_qc_bundle_core_files_missing")
    if _sha256_file(root / "final.mp4") != str(manifest.get("final_sha256") or "").strip().lower():
        raise AudioQCPendingBundleError("audio_qc_bundle_manifest_final_mismatch")
    checkpoint, semantic = _validate_source_checkpoint(root)
    if str(checkpoint["source"].get("run_id") or "") != str(source.get("run_id") or ""):
        raise AudioQCPendingBundleError("audio_qc_bundle_checkpoint_source_mismatch")
    if semantic.get("production_id") != source.get("production_id"):
        raise AudioQCPendingBundleError("audio_qc_bundle_semantic_source_mismatch")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--output-dir", required=True, type=Path)
    build.add_argument("--bundle", required=True, type=Path)
    verify = sub.add_parser("verify")
    verify.add_argument("--bundle", required=True, type=Path)
    verify.add_argument("--source-run-id")
    verify.add_argument("--runner-sha")
    verify.add_argument("--engine-sha")
    args = parser.parse_args()
    if args.command == "build":
        manifest = build_resume_bundle(args.output_dir, args.bundle)
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

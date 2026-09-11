from __future__ import annotations

"""Immutable parent-master contract for post-Gold delivery.

Once a long-form final has passed Final Master and Gold on the same render, downstream
packaging, release staging, observers, and derived-media work must never be able to
silently mutate or invalidate that accepted parent.  This module records the exact
media/evidence identities and provides a fail-closed assertion seam for every post-Gold
owner.
"""

import hashlib
import json
from pathlib import Path
from typing import Any

from scripts.final_master_acceptance_v2 import require_final_master_acceptance

SCHEMA_VERSION = 1
CONTRACT_ID = "post_gold.parent_master_lock.v1"
LOCK_FILE = "master-lock.json"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _identity(path: Path) -> dict[str, Any]:
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f"Master lock requires regular file: {path.name}")
    return {
        "file": path.name,
        "size": path.stat().st_size,
        "sha256": _sha256_file(path),
    }


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Master lock source is invalid JSON: {Path(path).name}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"Master lock source must be an object: {Path(path).name}")
    return value


def _require_gold_same_render(root: Path) -> dict[str, Any]:
    report = _read_object(Path(root) / "gold-enforce-report.json")
    viewer = report.get("viewer_quality")
    same_render = report.get("same_render")
    if not (
        report.get("phase") == "4"
        and report.get("mode") == "enforce"
        and isinstance(report.get("gold"), dict)
        and report["gold"].get("accepted") is True
        and isinstance(viewer, dict)
        and viewer.get("verdict") == "pass"
        and isinstance(same_render, dict)
        and same_render.get("artifact_divergence") is False
    ):
        raise RuntimeError("Master lock requires accepted Gold + Viewer Quality on the same render")
    return report


def _request_identity(request: dict[str, Any] | None) -> dict[str, Any] | None:
    if not request:
        return None
    return {
        "request_id": request.get("request_id"),
        "request_sha256": request.get("request_sha256"),
        "kind": request.get("kind"),
        "approval_scope": request.get("approval_scope"),
        "approved_topic": request.get("approved_topic"),
    }


def _source_identity(source: dict[str, Any] | None) -> dict[str, Any] | None:
    if not source:
        return None
    return {
        "run_id": source.get("run_id"),
        "run_attempt": source.get("run_attempt"),
        "runner_sha": source.get("runner_sha"),
        "engine_sha": source.get("engine_sha"),
    }


def _build_document(
    root: Path,
    *,
    request: dict[str, Any] | None,
    source: dict[str, Any] | None,
    release_tag: str | None,
    expected_final_sha: str | None,
) -> dict[str, Any]:
    root = Path(root)
    final = root / "final.mp4"
    final_master_qc_path = root / "final-master-qc.json"
    gold_path = root / "gold-enforce-report.json"

    final_master_qc = require_final_master_acceptance(root)
    _require_gold_same_render(root)

    final_identity = _identity(final)
    qc_identity = _identity(final_master_qc_path)
    gold_identity = _identity(gold_path)
    expected = str(expected_final_sha or "").strip().lower()
    if expected and final_identity["sha256"] != expected:
        raise RuntimeError("Master lock final.mp4 differs from the authorized source hash")

    accepted_sha = str(
        (((final_master_qc.get("acceptance_contract") or {}).get("sources") or {}).get("final") or {}).get("sha256")
        or ""
    ).strip().lower()
    if not accepted_sha or accepted_sha != final_identity["sha256"]:
        raise RuntimeError("Master lock Final Master evidence is not bound to current final.mp4")

    return {
        "schema_version": SCHEMA_VERSION,
        "contract_id": CONTRACT_ID,
        "state": "locked_after_gold",
        "mutation_allowed": False,
        "parent_media_rebuild_allowed": False,
        "final": final_identity,
        "evidence": {
            "final_master_qc": qc_identity,
            "gold_enforce_report": gold_identity,
        },
        "request": _request_identity(request),
        "source": _source_identity(source),
        "release_candidate_tag": str(release_tag or "").strip() or None,
    }


def assert_master_lock(root: Path, *, expected_final_sha: str | None = None) -> dict[str, Any]:
    root = Path(root)
    path = root / LOCK_FILE
    if not path.is_file():
        raise RuntimeError("Post-Gold parent master lock is missing")
    lock = _read_object(path)
    if (
        lock.get("schema_version") != SCHEMA_VERSION
        or lock.get("contract_id") != CONTRACT_ID
        or lock.get("state") != "locked_after_gold"
        or lock.get("mutation_allowed") is not False
        or lock.get("parent_media_rebuild_allowed") is not False
    ):
        raise RuntimeError("Post-Gold parent master lock contract is invalid")

    # Re-run authoritative acceptance before comparing identities. This catches a final
    # mutation even if a stale lock file was copied alongside it.
    require_final_master_acceptance(root)
    _require_gold_same_render(root)

    expected_identities = {
        "final": _identity(root / "final.mp4"),
        "final_master_qc": _identity(root / "final-master-qc.json"),
        "gold_enforce_report": _identity(root / "gold-enforce-report.json"),
    }
    if lock.get("final") != expected_identities["final"]:
        raise RuntimeError("Post-Gold parent final.mp4 changed after master lock")
    evidence = lock.get("evidence") if isinstance(lock.get("evidence"), dict) else {}
    if evidence.get("final_master_qc") != expected_identities["final_master_qc"]:
        raise RuntimeError("Post-Gold Final Master evidence changed after master lock")
    if evidence.get("gold_enforce_report") != expected_identities["gold_enforce_report"]:
        raise RuntimeError("Post-Gold Gold evidence changed after master lock")

    expected = str(expected_final_sha or "").strip().lower()
    if expected and expected_identities["final"]["sha256"] != expected:
        raise RuntimeError("Post-Gold parent final hash no longer matches authorized source")
    return lock


def write_master_lock(
    root: Path,
    *,
    request: dict[str, Any] | None = None,
    source: dict[str, Any] | None = None,
    release_tag: str | None = None,
    expected_final_sha: str | None = None,
) -> Path:
    root = Path(root)
    path = root / LOCK_FILE
    document = _build_document(
        root,
        request=request,
        source=source,
        release_tag=release_tag,
        expected_final_sha=expected_final_sha,
    )
    if path.is_file():
        existing = _read_object(path)
        if existing != document:
            raise RuntimeError("Existing post-Gold master lock conflicts with current accepted parent")
        assert_master_lock(root, expected_final_sha=expected_final_sha)
        return path

    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    assert_master_lock(root, expected_final_sha=expected_final_sha)
    return path

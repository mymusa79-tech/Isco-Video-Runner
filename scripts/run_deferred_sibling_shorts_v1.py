from __future__ import annotations

"""Execute sibling Shorts as an isolated derivative job after parent delivery.

This worker consumes only the sealed derivative contract exported by the accepted parent.
It never owns, rebuilds, republishes, or changes the parent long-form master. Provider
failures are therefore contained to this child job and can be retried independently.
"""

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from isco_video_agent.config import secret

from scripts.run_control_production import execute_child_subprocess
from scripts.short_finishing_capabilities import ShortFinishingCapabilities
from scripts.sibling_short_orchestration import orchestrate_sibling_shorts, stage_sibling_assets

DEFERRED_CONTRACT_ID = "post_gold.sibling_short_deferred.v1"
PARENT_CONTRACT_ID = "post_gold.sibling_parent_contract.v1"
RESULT_CONTRACT_ID = "post_gold.sibling_derivative_result.v1"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_object(path: Path) -> dict[str, Any]:
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f"Deferred sibling worker missing safe input: {path.name}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Deferred sibling worker invalid JSON: {path.name}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"Deferred sibling worker requires JSON object: {path.name}")
    return value


def _assert_binding(root: Path, descriptor: dict[str, Any], expected_name: str) -> Path:
    if not isinstance(descriptor, dict):
        raise RuntimeError(f"Deferred sibling contract lost {expected_name} binding")
    name = str(descriptor.get("file") or "").strip()
    expected_sha = str(descriptor.get("sha256") or "").strip().lower()
    if name != expected_name or len(expected_sha) != 64:
        raise RuntimeError(f"Deferred sibling contract has invalid {expected_name} binding")
    path = Path(root) / name
    if _sha256_file(path) != expected_sha:
        raise RuntimeError(f"Deferred sibling {expected_name} changed after parent delivery")
    return path


def validate_deferred_parent(root: Path, *, parent_release_tag: str) -> tuple[dict[str, Any], Path, dict[str, Any]]:
    root = Path(root)
    deferred = _read_object(root / "sibling-short-deferred.json")
    if (
        deferred.get("contract_id") != DEFERRED_CONTRACT_ID
        or deferred.get("status") != "deferred_after_parent_gold"
        or deferred.get("blocking_parent_delivery") is not False
        or deferred.get("automatic_production_started") is not False
        or deferred.get("provider_calls_performed") is not False
        or deferred.get("partial_child_delivery_allowed") is not False
        or deferred.get("execution_owner") != "isolated_child_jobs"
    ):
        raise RuntimeError("Deferred sibling execution contract is not eligible for isolated child production")

    master_lock_path = _assert_binding(root, deferred.get("master_lock") or {}, "master-lock.json")
    parent_contract_path = _assert_binding(
        root, deferred.get("parent_contract") or {}, "sibling-short-parent-contract.json"
    )
    sibling_plan_path = _assert_binding(root, deferred.get("sibling_short_plan") or {}, "sibling-short-plan.json")

    master_lock = _read_object(master_lock_path)
    if (
        master_lock.get("contract_id") != "post_gold.parent_master_lock.v1"
        or master_lock.get("state") != "locked_after_gold"
        or master_lock.get("mutation_allowed") is not False
        or master_lock.get("parent_media_rebuild_allowed") is not False
    ):
        raise RuntimeError("Deferred sibling worker requires an immutable accepted parent master")
    parent_final_sha = str(((master_lock.get("final") or {}).get("sha256")) or "").strip().lower()
    if parent_final_sha != str(deferred.get("parent_final_sha256") or "").strip().lower():
        raise RuntimeError("Deferred sibling contract is not bound to the locked parent final")

    parent = _read_object(parent_contract_path)
    if (
        parent.get("contract_id") != PARENT_CONTRACT_ID
        or parent.get("approved_by_user") is not True
        or parent.get("kind") != "long"
        or parent.get("approval_scope") != "long_plus_sibling_shorts"
        or parent.get("production_dispatch_authorized") is not False
        or parent.get("request_id") != deferred.get("parent_request_id")
        or parent.get("request_sha256") != deferred.get("parent_request_sha256")
    ):
        raise RuntimeError("Deferred sibling parent contract is invalid or approval binding changed")

    plan = _read_object(sibling_plan_path)
    planned_count = int(plan.get("short_count") or 0)
    expected_count = int((deferred.get("sibling_short_plan") or {}).get("short_count") or 0)
    if planned_count != expected_count or planned_count not in {2, 3}:
        raise RuntimeError("Deferred sibling plan count changed after parent delivery")

    release_tag = str(parent_release_tag or "").strip()
    if not release_tag:
        raise RuntimeError("Deferred sibling worker requires parent release tag")
    locked_tag = str(master_lock.get("release_candidate_tag") or "").strip()
    if locked_tag and locked_tag != release_tag:
        raise RuntimeError("Deferred sibling worker parent release tag differs from master lock")
    return parent, sibling_plan_path, master_lock


def execute_deferred_siblings(
    *,
    parent_dir: Path,
    parent_release_tag: str,
    runtime_root: Path,
    output_dir: Path,
    result_path: Path,
) -> dict[str, Any]:
    parent_dir = Path(parent_dir).resolve()
    parent, sibling_plan_path, master_lock = validate_deferred_parent(
        parent_dir, parent_release_tag=parent_release_tag
    )

    gemini = secret("GEMINI_API_KEY")
    pexels = secret("PEXELS_API_KEY")
    pixabay = secret("PIXABAY_API_KEY")
    if not gemini or not pexels:
        raise RuntimeError("Deferred sibling child production requires Gemini and Pexels capabilities")
    capabilities = ShortFinishingCapabilities(gemini=gemini, pexels=pexels, pixabay=pixabay)

    def execute_child(child_request: dict[str, Any]) -> Path:
        return execute_child_subprocess(
            child_request,
            runtime_root=Path(runtime_root),
            capabilities=capabilities,
        )

    completed = orchestrate_sibling_shorts(parent, sibling_plan_path, execute_short=execute_child)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    staged = stage_sibling_assets(output_dir, completed)
    if len(staged) not in {2, 3}:
        raise RuntimeError("Deferred sibling child job ended without the complete approved set")

    result = {
        "schema_version": 1,
        "contract_id": RESULT_CONTRACT_ID,
        "status": "complete",
        "parent_release_tag": parent_release_tag,
        "parent_request_id": parent.get("request_id"),
        "parent_request_sha256": parent.get("request_sha256"),
        "parent_final_sha256": ((master_lock.get("final") or {}).get("sha256")),
        "parent_media_rebuilt": False,
        "parent_delivery_blocked": False,
        "short_count": len(staged),
        "shorts": staged,
        "partial_delivery_allowed": False,
        "execution_mode": "post_parent_delivery_sequential_isolated_subprocesses",
        "youtube_publish_mode": "manual_in_youtube_studio",
    }
    result_path = Path(result_path)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (output_dir / "sibling-short-derived-delivery.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent-dir", required=True, type=Path)
    parser.add_argument("--parent-release-tag", required=True)
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--result", required=True, type=Path)
    args = parser.parse_args()
    execute_deferred_siblings(
        parent_dir=args.parent_dir,
        parent_release_tag=args.parent_release_tag,
        runtime_root=args.runtime_root,
        output_dir=args.output_dir,
        result_path=args.result,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

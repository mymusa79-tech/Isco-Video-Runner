from __future__ import annotations

"""Finish an accepted QC_PENDING source without letting downstream work own the parent.

The long-form parent becomes immutable immediately after successful Gold. Packaging and
release staging may continue over those exact bytes, while approved sibling Shorts are
recorded as deferred isolated child work. No provider-backed child production or observer
call is allowed to sit between parent Gold acceptance and parent delivery.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import scripts.run_v3_voice as production
from scripts.orchestration_shorts_port import finalize_short_quality
from scripts.post_gold_master_lock_v1 import assert_master_lock, write_master_lock
from scripts.run_control_production import validate_control_request, write_sibling_short_plan
from scripts.runtime_closure import run_post_gold_observers
from scripts.unified_delivery import write_delivery_manifest


CONTRACT_ID = "gold.qc-pending.post-gold-delivery.v1"
DEFERRED_SIBLING_CONTRACT_ID = "post_gold.sibling_short_deferred.v1"
SIBLING_PARENT_CONTRACT_ID = "post_gold.sibling_parent_contract.v1"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"Post-Gold continuation requires JSON object: {Path(path).name}")
    return value


def _gold_is_accepted(root: Path) -> dict[str, Any]:
    report = _read_object(root / "gold-enforce-report.json")
    viewer = report.get("viewer_quality")
    if not (
        report.get("phase") == "4"
        and report.get("mode") == "enforce"
        and isinstance(report.get("gold"), dict)
        and report["gold"].get("accepted") is True
        and isinstance(viewer, dict)
        and viewer.get("verdict") == "pass"
        and isinstance(report.get("same_render"), dict)
        and report["same_render"].get("artifact_divergence") is False
    ):
        raise RuntimeError("Post-Gold continuation requires accepted Gold + Viewer Quality on same render")
    return report


def _write_resume_production_manifest(
    root: Path,
    *,
    fmt: str,
    release_tag: str,
    source: dict[str, Any],
) -> dict[str, Any]:
    source_run_id = str(source.get("run_id") or "").strip()
    source_attempt = str(source.get("run_attempt") or "").strip()
    source_runner_sha = str(source.get("runner_sha") or "").strip().lower()
    source_engine_sha = str(source.get("engine_sha") or "").strip().lower()
    if not source_run_id or not source_attempt:
        raise RuntimeError("Post-Gold continuation lost source production identity")
    if len(source_runner_sha) != 40 or any(ch not in "0123456789abcdef" for ch in source_runner_sha):
        raise RuntimeError("Post-Gold continuation lost exact source Runner SHA")
    if len(source_engine_sha) != 40 or any(ch not in "0123456789abcdef" for ch in source_engine_sha):
        raise RuntimeError("Post-Gold continuation lost exact source Engine SHA")

    resume_execution = {
        "run_id": str(os.environ.get("GITHUB_RUN_ID") or "").strip() or None,
        "run_number": str(os.environ.get("GITHUB_RUN_NUMBER") or "").strip() or None,
        "run_attempt": str(os.environ.get("GITHUB_RUN_ATTEMPT") or "").strip() or None,
        "runner_sha": str(os.environ.get("GITHUB_SHA") or "").strip().lower() or None,
        "engine_sha": str(os.environ.get("ISCO_ENGINE_SHA") or "").strip().lower() or None,
        "parent_media_rebuilt": False,
    }

    old_tag = os.environ.get("ISCO_RELEASE_TAG_OVERRIDE")
    try:
        os.environ["ISCO_RELEASE_TAG_OVERRIDE"] = release_tag
        manifest = production._write_production_manifest(
            root,
            production_id=f"v4:{source_run_id}:{source_attempt}",
            fmt=fmt,
        )
    finally:
        if old_tag is None:
            os.environ.pop("ISCO_RELEASE_TAG_OVERRIDE", None)
        else:
            os.environ["ISCO_RELEASE_TAG_OVERRIDE"] = old_tag

    manifest["github_run_id"] = source_run_id
    manifest["github_run_number"] = None
    manifest["github_run_attempt"] = source_attempt
    manifest["runner_sha"] = source_runner_sha
    manifest["engine_sha"] = source_engine_sha
    manifest["resume_source"] = {
        "run_id": source_run_id,
        "run_attempt": source_attempt,
        "runner_sha": source_runner_sha,
        "engine_sha": source_engine_sha,
    }
    manifest["resume_execution"] = resume_execution
    (root / "production-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def _finalize_standalone_short(root: Path, request: dict[str, Any]) -> None:
    pre = _read_object(root / "short-intelligence-pre-gold.json")
    runtime_request = dict(request)
    runtime_request["production_dispatch_authorized"] = True
    finalize_short_quality(root, runtime_request, pre)


def _write_sibling_parent_contract(root: Path, request: dict[str, Any]) -> Path:
    candidate = request.get("candidate")
    if not isinstance(candidate, dict) or not candidate:
        raise RuntimeError("Deferred sibling continuation requires approved parent candidate evidence")
    document = {
        "schema_version": 1,
        "contract_id": SIBLING_PARENT_CONTRACT_ID,
        "approved_by_user": True,
        "kind": "long",
        "approval_scope": "long_plus_sibling_shorts",
        "production_dispatch_authorized": False,
        "request_id": request.get("request_id"),
        "request_sha256": request.get("request_sha256"),
        "approved_topic": request.get("approved_topic"),
        "approved_at": request.get("approved_at"),
        "weekly_option_id": request.get("weekly_option_id"),
        "content_boundaries": list(request.get("content_boundaries") or []),
        "candidate": dict(candidate),
        "sibling_shorts": dict(request.get("sibling_shorts") or {}),
        "source": request.get("source"),
        "status": request.get("status"),
        "youtube_publish_mode": "manual_in_youtube_studio",
    }
    required = ("request_id", "request_sha256", "approved_topic")
    if any(not str(document.get(key) or "").strip() for key in required):
        raise RuntimeError("Deferred sibling parent contract lost approval provenance")
    path = root / "sibling-short-parent-contract.json"
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _defer_approved_sibling_shorts(
    root: Path,
    request: dict[str, Any],
    *,
    parent_final_sha256: str,
) -> Path:
    sibling_plan = write_sibling_short_plan(root, request)
    if sibling_plan is None or not sibling_plan.is_file():
        raise RuntimeError("Post-Gold long+Shorts continuation produced no sibling plan")
    plan = _read_object(sibling_plan)
    count = int(plan.get("short_count") or 0)
    if count not in {2, 3}:
        raise RuntimeError("Deferred sibling continuation must preserve the approved 2–3 Short quota")

    lock_path = root / "master-lock.json"
    if not lock_path.is_file():
        raise RuntimeError("Deferred sibling continuation requires locked parent master")
    parent_contract = _write_sibling_parent_contract(root, request)
    document = {
        "schema_version": 1,
        "contract_id": DEFERRED_SIBLING_CONTRACT_ID,
        "status": "deferred_after_parent_gold",
        "parent_request_id": request.get("request_id"),
        "parent_request_sha256": request.get("request_sha256"),
        "parent_final_sha256": parent_final_sha256,
        "master_lock": {
            "file": lock_path.name,
            "sha256": _sha256_file(lock_path),
        },
        "parent_contract": {
            "file": parent_contract.name,
            "sha256": _sha256_file(parent_contract),
        },
        "sibling_short_plan": {
            "file": sibling_plan.name,
            "sha256": _sha256_file(sibling_plan),
            "short_count": count,
        },
        "execution_owner": "isolated_child_jobs",
        "automatic_production_started": False,
        "provider_calls_performed": False,
        "blocking_parent_delivery": False,
        "partial_child_delivery_allowed": False,
        "youtube_publish_mode": "manual_in_youtube_studio",
    }
    path = root / "sibling-short-deferred.json"
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def finalize_after_gold_resume(
    *,
    output_dir: Path,
    request_path: Path,
    resume_manifest_path: Path,
    release_tag: str,
    runtime_root: Path,
    result_output: Path,
) -> dict[str, Any]:
    root = Path(output_dir).resolve()
    request = _read_object(request_path)
    validate_control_request(request, str(request.get("request_sha256") or ""))
    resume_manifest = _read_object(resume_manifest_path)
    source = resume_manifest.get("source")
    if not isinstance(source, dict):
        raise RuntimeError("Post-Gold continuation resume manifest has no source identity")

    release_tag = str(release_tag or "").strip()
    if not release_tag:
        raise RuntimeError("Post-Gold continuation requires the original release candidate tag")
    final_path = root / "final.mp4"
    if not final_path.is_file():
        raise RuntimeError("Post-Gold continuation cannot find parent final.mp4")
    final_sha_before = _sha256_file(final_path)
    expected_final = str(resume_manifest.get("final_sha256") or "").strip().lower()
    if final_sha_before != expected_final:
        raise RuntimeError("Post-Gold continuation parent final does not match resume source")

    _gold_is_accepted(root)
    fmt = str(_read_object(root / "plan.json").get("format") or "").strip().lower()
    kind = str(request.get("kind") or "").strip()
    scope = str(request.get("approval_scope") or "").strip()
    if kind == "short" and fmt != "moment":
        raise RuntimeError("Post-Gold standalone Short escaped moment format")
    if kind == "long" and fmt not in {"film", "story"}:
        raise RuntimeError("Post-Gold long-form continuation has unsupported format")

    deferred_siblings: Path | None = None
    if kind == "short":
        _finalize_standalone_short(root, request)
        staged_shorts: list[dict[str, Any]] = []
        # Preserve the existing Short observer path. The long-form parent path below is
        # deliberately provider-free once Gold has accepted the immutable master.
        run_post_gold_observers(root)
    elif scope in {"long_only", "long_plus_sibling_shorts"}:
        write_master_lock(
            root,
            request=request,
            source=source,
            release_tag=release_tag,
            expected_final_sha=expected_final,
        )
        staged_shorts = []
        if scope == "long_plus_sibling_shorts":
            deferred_siblings = _defer_approved_sibling_shorts(
                root,
                request,
                parent_final_sha256=final_sha_before,
            )
    else:
        raise RuntimeError("Post-Gold continuation approval scope is unsupported")

    # Long-form work below this line is deterministic/local with respect to the accepted
    # parent: no Gemini/Groq/OpenRouter/Pexels/Pixabay/Piper call is permitted here.
    production_manifest = _write_resume_production_manifest(
        root,
        fmt=fmt,
        release_tag=release_tag,
        source=source,
    )
    delivery_path = write_delivery_manifest(
        root,
        repository=(os.environ.get("GITHUB_REPOSITORY") or "mymusa79-tech/Isco-Video-Runner").strip(),
        release_tag=release_tag,
        request=request,
        short_assets=staged_shorts,
    )

    final_sha_after = _sha256_file(final_path)
    if final_sha_after != final_sha_before:
        raise RuntimeError("Post-Gold continuation mutated the parent final.mp4")
    if kind == "long":
        assert_master_lock(root, expected_final_sha=final_sha_before)

    result = {
        "schema_version": 1,
        "contract_id": CONTRACT_ID,
        "status": "delivery_staged",
        "request_id": request.get("request_id"),
        "kind": kind,
        "approval_scope": scope,
        "format": fmt,
        "release_tag": release_tag,
        "source_run_id": source.get("run_id"),
        "parent_final_sha256": final_sha_after,
        "parent_media_rebuilt": False,
        "master_lock": str(root / "master-lock.json") if kind == "long" else None,
        "sibling_shorts_produced_after_parent_gold": 0,
        "sibling_short_continuation": str(deferred_siblings) if deferred_siblings else None,
        "production_manifest": str(root / "production-manifest.json"),
        "delivery_manifest": str(delivery_path),
        "delivery_kind": _read_object(delivery_path).get("delivery_kind"),
        "release_authority": production_manifest.get("release_authority"),
    }
    result_output.parent.mkdir(parents=True, exist_ok=True)
    result_output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--resume-manifest", required=True, type=Path)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--result", required=True, type=Path)
    args = parser.parse_args()
    finalize_after_gold_resume(
        output_dir=args.output_dir,
        request_path=args.request,
        resume_manifest_path=args.resume_manifest,
        release_tag=args.release_tag,
        runtime_root=args.runtime_root,
        result_output=args.result,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

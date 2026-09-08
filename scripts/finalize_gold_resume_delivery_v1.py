from __future__ import annotations

"""Finish the same approved Telegram scope after a successful QC_PENDING Gold resume.

This owner starts *after* the parent final.mp4 has already passed Final Master QC and a
subsequent Gold resume.  It must never replan, research, retrieve media, resynthesize, or
rerender that parent.  It performs only the post-Gold work the original control request
would have performed if Gold had not been temporarily unavailable:

- standalone Short: project the already-persisted pre-Gold Short intelligence through
  the final Short quality contract;
- Long only: stage the reviewed long-form delivery;
- Long + sibling Shorts: create the already-approved 2-3 derived Shorts, then stage the
  unified delivery;
- every scope: write production/delivery provenance over the exact unchanged parent.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from isco_video_agent.config import secret

import scripts.run_v3_voice as production
from scripts.orchestration_shorts_port import finalize_short_quality
from scripts.run_control_production import (
    execute_child_subprocess,
    validate_control_request,
    write_sibling_short_plan,
)
from scripts.runtime_closure import run_post_gold_observers
from scripts.short_finishing_capabilities import ShortFinishingCapabilities
from scripts.sibling_short_orchestration import orchestrate_sibling_shorts, stage_sibling_assets
from scripts.unified_delivery import write_delivery_manifest


CONTRACT_ID = "gold.qc-pending.post-gold-delivery.v1"


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

    # production-manifest.v1 has an implicit identity invariant on the ordinary path:
    # production_id, github_run_id/attempt and Runner/Engine SHAs all describe the same
    # production that created final.mp4. A Gold-only resume must preserve that historical
    # identity at the top level; the current workflow is an acceptance execution, not a
    # new media production. Keep its identity separately under resume_execution.
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
    # The v2 resume bundle intentionally did not persist source run_number. Never copy
    # the current resume run number into historical production provenance; release_tag is
    # already explicitly bound to the original candidate by the resume authorization.
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


def _produce_approved_sibling_shorts(
    root: Path,
    request: dict[str, Any],
    *,
    runtime_root: Path,
) -> list[dict[str, Any]]:
    sibling_plan = write_sibling_short_plan(root, request)
    if sibling_plan is None or not sibling_plan.is_file():
        raise RuntimeError("Post-Gold long+Shorts continuation produced no sibling plan")

    gemini = secret("GEMINI_API_KEY")
    pexels = secret("PEXELS_API_KEY")
    pixabay = secret("PIXABAY_API_KEY")
    if not gemini or not pexels:
        raise RuntimeError("Post-Gold sibling continuation requires Gemini and Pexels capabilities")
    capabilities = ShortFinishingCapabilities(gemini=gemini, pexels=pexels, pixabay=pixabay)

    def execute_short(child_request: dict[str, Any]) -> Path:
        return execute_child_subprocess(
            child_request,
            runtime_root=runtime_root,
            capabilities=capabilities,
        )

    completed = orchestrate_sibling_shorts(request, sibling_plan, execute_short=execute_short)
    staged = stage_sibling_assets(root, completed)
    (root / "sibling-short-results.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "parent_request_id": request.get("request_id"),
                "parent_request_sha256": request.get("request_sha256"),
                "short_count": len(staged),
                "shorts": staged,
                "execution_mode": "gold_resume_then_sequential_isolated_subprocesses",
                "short_source_mode": "exact_long_episode_sections",
                "partial_delivery_allowed": False,
                "youtube_publish_mode": "manual_in_youtube_studio",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return staged


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

    if kind == "short":
        _finalize_standalone_short(root, request)
        staged_shorts: list[dict[str, Any]] = []
    elif scope == "long_plus_sibling_shorts":
        staged_shorts = _produce_approved_sibling_shorts(
            root,
            request,
            runtime_root=Path(runtime_root),
        )
    elif scope == "long_only":
        staged_shorts = []
    else:
        raise RuntimeError("Post-Gold continuation approval scope is unsupported")

    # Match the normal successful path: observers are non-authoritative and may skip,
    # while production/delivery manifests are enforcing deterministic provenance.
    run_post_gold_observers(root)
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
        "sibling_shorts_produced_after_parent_gold": len(staged_shorts),
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

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from isco_video_agent.brief_approval_binding import attach_approval_binding

from scripts.orchestration_shorts_port import finalize_short_quality, prepare_authoritative_short_for_gold
from scripts.short_finishing_capabilities import (
    ShortFinishingCapabilities,
    bind_short_finishing_capabilities,
)
from scripts.source_derived_short_planner import install_source_derived_short_planner
from scripts.source_derived_visual_capsule import build_parent_visual_capsule


SOURCE = "canonical_v4_approved_brief"
PARENT_VISUAL_ROOT_ENV = "ISCO_SOURCE_PARENT_OUTPUT_DIR"


def _canonical_hash(document: dict[str, Any]) -> str:
    subject = {key: value for key, value in document.items() if key != "request_sha256"}
    payload = json.dumps(subject, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate_request(request: dict[str, Any], expected_sha256: str) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise RuntimeError("Canonical V4 sibling request must be an object")
    stored = str(request.get("request_sha256") or "")
    if not stored or stored != _canonical_hash(request) or stored != str(expected_sha256 or ""):
        raise RuntimeError("Canonical V4 sibling request changed after approval inheritance")
    if request.get("source") != SOURCE:
        raise RuntimeError("Canonical V4 sibling request has an unsupported source")
    if request.get("kind") != "short" or request.get("format") != "moment":
        raise RuntimeError("Canonical V4 sibling request must stay a moment Short")
    if request.get("approval_scope") != "short_sibling":
        raise RuntimeError("Canonical V4 sibling request escaped short_sibling scope")
    if request.get("approved_by_user") is not True or request.get("approval_inherited_from_approved_brief") is not True:
        raise RuntimeError("Canonical V4 sibling request lacks inherited user approval")
    if request.get("production_dispatch_authorized") is not False:
        raise RuntimeError("Stored canonical V4 sibling request must remain non-dispatching")
    if request.get("status") != "approved_waiting_production_activation":
        raise RuntimeError("Canonical V4 sibling request has an invalid stored status")
    if request.get("youtube_publish_mode") != "manual_in_youtube_studio":
        raise RuntimeError("Canonical V4 sibling request attempted to change manual YouTube publication")
    if not str(request.get("parent_approved_brief_sha256") or "").strip():
        raise RuntimeError("Canonical V4 sibling request lacks parent approved-brief provenance")
    excerpt = request.get("source_episode_excerpt")
    if not isinstance(excerpt, dict) or not str(excerpt.get("source_section_id") or "").strip():
        raise RuntimeError("Canonical V4 sibling request lacks exact parent section provenance")
    return request


def _output_dirs() -> set[Path]:
    return {path.resolve() for path in Path("output").glob("*") if path.is_dir()}


def _new_output_dir(before: set[Path]) -> Path:
    created = [path.resolve() for path in Path("output").glob("*") if path.is_dir() and path.resolve() not in before]
    if len(created) != 1:
        raise RuntimeError(f"Canonical V4 sibling expected exactly one new output directory, found {len(created)}")
    return created[0]


def _materialize_brief(request: dict[str, Any], path: Path) -> tuple[Path, str]:
    brief = attach_approval_binding(
        {
            "approved_by_user": True,
            "approved_topic": str(request.get("approved_topic") or "").strip(),
            "format": "moment",
            "approved_at": request.get("approved_at"),
            "weekly_option_id": request.get("weekly_option_id"),
            "research_pack": [],
            "content_boundaries": list(request.get("content_boundaries") or []),
            "canonical_bundle_request_id": request.get("request_id"),
            "canonical_bundle_request_sha256": request.get("request_sha256"),
            "parent_approved_brief_sha256": request.get("parent_approved_brief_sha256"),
        }
    )
    if not brief["approved_topic"]:
        raise RuntimeError("Canonical V4 sibling request has no approved semantic job")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(brief, ensure_ascii=False, indent=2), encoding="utf-8")
    return path, str(brief["approved_hash"])


def _resolve_parent_visual_root() -> Path:
    """Capture the source long output before the child replaces its approved-brief env."""
    explicit = str(os.environ.get(PARENT_VISUAL_ROOT_ENV) or "").strip()
    if explicit:
        root = Path(explicit).resolve()
    else:
        source_brief = str(os.environ.get("ISCO_APPROVED_BRIEF_PATH") or "").strip()
        if not source_brief:
            raise RuntimeError("Canonical sibling Short cannot resolve the source long output directory")
        root = Path(source_brief).resolve().parent
    required = (root / "picture.mp4", root / "final.mp4", root / "visual-timeline.json", root / "rights-manifest.json")
    if not all(path.is_file() for path in required):
        raise RuntimeError("Canonical sibling Short source long output lacks certified video inheritance artifacts")
    return root


def execute(request: dict[str, Any], *, runtime_dir: Path) -> Path:
    # Import the full production graph only when an actual child execution begins.
    import scripts.planning_runtime_contract as planning_runtime_contract
    import scripts.run_v3_voice as production

    validate_request(request, str(request.get("request_sha256") or ""))
    parent_visual_root = _resolve_parent_visual_root()
    source_section_id = str((request.get("source_episode_excerpt") or {}).get("source_section_id") or "").strip()
    visual_capsule = build_parent_visual_capsule(parent_visual_root, source_section_id)

    runtime_dir = Path(runtime_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    brief_path, brief_sha = _materialize_brief(request, runtime_dir / "approved-brief.json")
    request_file = runtime_dir / "isco-request.json"
    request_file.write_text(
        json.dumps({"topic": request["approved_topic"], "format": "moment"}, ensure_ascii=False),
        encoding="utf-8",
    )

    previous_env = {
        key: os.environ.get(key)
        for key in (
            "ISCO_APPROVED_BRIEF_PATH",
            "ISCO_APPROVED_BRIEF_SHA256",
            "REQUEST_FILE",
            "ISCO_CONTROL_REQUEST_ID",
            PARENT_VISUAL_ROOT_ENV,
        )
    }
    os.environ["ISCO_APPROVED_BRIEF_PATH"] = str(brief_path.resolve())
    os.environ["ISCO_APPROVED_BRIEF_SHA256"] = brief_sha
    os.environ["REQUEST_FILE"] = str(request_file.resolve())
    os.environ["ISCO_CONTROL_REQUEST_ID"] = str(request.get("request_id") or "")
    os.environ[PARENT_VISUAL_ROOT_ENV] = str(parent_visual_root)

    original_gold = production.run_gold_enforce_phase4
    original_install_router = planning_runtime_contract.install_router
    original_resolve_plan_source = production._resolve_plan_source
    original_budget_factory = production._production_budget_ledger
    runtime_request = dict(request)
    runtime_request["production_dispatch_authorized"] = True
    runtime_request["source_visual_capsule"] = visual_capsule
    short_pre: dict[str, Any] | None = None
    ledger_box: dict[str, Any] = {}
    before = _output_dirs()

    def captured_budget_factory(fmt: str):
        ledger = original_budget_factory(fmt)
        ledger_box["ledger"] = ledger
        return ledger

    def controlled_gold(**kwargs):
        nonlocal short_pre
        output_dir = Path(kwargs["output_dir"])
        ledger = ledger_box.get("ledger")
        if ledger is None:
            raise RuntimeError("Canonical sibling Short lost the production AI budget ledger before finishing")
        capabilities = ShortFinishingCapabilities.from_gold_kwargs(kwargs)
        with bind_short_finishing_capabilities(capabilities):
            short_pre = prepare_authoritative_short_for_gold(
                output_dir,
                runtime_request,
                ledger=ledger,
                run_final_master_qc=production.run_final_master_qc,
            )
        result = original_gold(**kwargs)
        assert short_pre is not None
        finalize_short_quality(output_dir, runtime_request, short_pre)
        return result

    production.run_gold_enforce_phase4 = controlled_gold
    production._production_budget_ledger = captured_budget_factory
    planning_runtime_contract.install_router = lambda: install_source_derived_short_planner(runtime_request)
    production._resolve_plan_source = lambda: "source_derived_long_episode_video_short"
    try:
        production.main()
        return _new_output_dir(before)
    finally:
        production.run_gold_enforce_phase4 = original_gold
        production._production_budget_ledger = original_budget_factory
        planning_runtime_contract.install_router = original_install_router
        production._resolve_plan_source = original_resolve_plan_source
        for key, value in previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--result", required=True, type=Path)
    args = parser.parse_args()
    request = json.loads(args.request.read_text(encoding="utf-8"))
    validate_request(request, args.sha256)
    runtime_root = Path(os.environ.get("RUNNER_TEMP") or ".") / "isco-canonical-v4-short" / str(request.get("request_id") or "short")
    output = execute(request, runtime_dir=runtime_root)
    args.result.parent.mkdir(parents=True, exist_ok=True)
    args.result.write_text(json.dumps({"output_dir": str(output)}, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
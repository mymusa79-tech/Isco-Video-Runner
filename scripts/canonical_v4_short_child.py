from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from isco_video_agent.brief_approval_binding import attach_approval_binding

from scripts.shorts_production_binding import finalize_short_quality, prepare_short_render
from scripts.source_derived_short_planner import install_source_derived_short_planner


SOURCE = "canonical_v4_approved_brief"


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


def execute(request: dict[str, Any], *, runtime_dir: Path) -> Path:
    # Import the full production graph only when an actual child execution begins.
    # Request validation and unit-contract imports must remain side-effect free and
    # must not depend on script-mode sys.path behavior used by the production entrypoint.
    import scripts.run_v3_voice as production

    validate_request(request, str(request.get("request_sha256") or ""))
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
        for key in ("ISCO_APPROVED_BRIEF_PATH", "ISCO_APPROVED_BRIEF_SHA256", "REQUEST_FILE", "ISCO_CONTROL_REQUEST_ID")
    }
    os.environ["ISCO_APPROVED_BRIEF_PATH"] = str(brief_path.resolve())
    os.environ["ISCO_APPROVED_BRIEF_SHA256"] = brief_sha
    os.environ["REQUEST_FILE"] = str(request_file.resolve())
    os.environ["ISCO_CONTROL_REQUEST_ID"] = str(request.get("request_id") or "")

    original_gold = production.run_gold_enforce_phase4
    original_install_router = production.install_router
    original_resolve_plan_source = production._resolve_plan_source
    runtime_request = dict(request)
    runtime_request["production_dispatch_authorized"] = True
    short_pre: dict[str, Any] | None = None
    before = _output_dirs()

    def controlled_gold(**kwargs):
        nonlocal short_pre
        short_pre = prepare_short_render(Path(kwargs["output_dir"]), runtime_request)
        result = original_gold(**kwargs)
        assert short_pre is not None
        finalize_short_quality(Path(kwargs["output_dir"]), runtime_request, short_pre)
        return result

    production.run_gold_enforce_phase4 = controlled_gold
    production.install_router = lambda: install_source_derived_short_planner(request)
    production._resolve_plan_source = lambda: "source_derived_long_episode_short"
    try:
        production.main()
        return _new_output_dir(before)
    finally:
        production.run_gold_enforce_phase4 = original_gold
        production.install_router = original_install_router
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

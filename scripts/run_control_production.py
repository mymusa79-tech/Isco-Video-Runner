from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from isco_video_agent.brief_approval_binding import attach_approval_binding
from isco_video_agent.short_planner import DEFAULT_SIBLING_SPACING_HOURS, select_sibling_jobs

import scripts.run_v3_voice as production
from scripts.shorts_production_binding import finalize_short_quality, prepare_short_render
from scripts.sibling_short_orchestration import orchestrate_sibling_shorts, stage_sibling_assets
from scripts.unified_delivery import write_delivery_manifest


def _canonical_request_hash(request: dict[str, Any]) -> str:
    subject = {key: value for key, value in request.items() if key != "request_sha256"}
    encoded = json.dumps(subject, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_control_request(request: dict[str, Any], expected_sha256: str) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise RuntimeError("Control request must be an object")
    stored = str(request.get("request_sha256") or "")
    current = _canonical_request_hash(request)
    if not stored or stored != current or stored != str(expected_sha256 or ""):
        raise RuntimeError("Control request changed after Telegram approval")
    if request.get("approved_by_user") is not True:
        raise RuntimeError("Control request lacks explicit user approval")
    if request.get("source") != "telegram_editorial_control_panel":
        raise RuntimeError("Unsupported control request source")
    if request.get("status") != "approved_waiting_production_activation":
        raise RuntimeError("Control request is not in an approved production-waiting state")
    if request.get("kind") not in {"long", "short"}:
        raise RuntimeError("Control request kind is unsupported")
    if request.get("production_dispatch_authorized") is not False:
        raise RuntimeError("Stored control request must remain non-dispatching")
    return request


def load_control_request(path: Path, expected_sha256: str) -> dict[str, Any]:
    try:
        request = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError("Control request is missing or invalid JSON") from exc
    return validate_control_request(request, expected_sha256)


def materialize_approved_brief(request: dict[str, Any], output: Path) -> tuple[Path, str]:
    fmt = "moment" if request.get("kind") == "short" else str(request.get("format") or "film")
    pack = request.get("approved_research_pack")
    if fmt in {"film", "story"}:
        if not isinstance(pack, list) or len(pack) < 2:
            raise RuntimeError("Long control production requires a completed approved research pack before dispatch")
    elif not isinstance(pack, list):
        pack = []
    brief = {
        "approved_by_user": True,
        "approved_topic": str(request.get("approved_topic") or "").strip(),
        "format": fmt,
        "approved_at": request.get("approved_at"),
        "weekly_option_id": request.get("weekly_option_id"),
        "research_pack": pack,
        "content_boundaries": request.get("content_boundaries") or [],
        "control_request_id": request.get("request_id"),
        "control_request_sha256": request.get("request_sha256"),
    }
    if not brief["approved_topic"]:
        raise RuntimeError("Control request has no approved topic")
    bound = attach_approval_binding(brief)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(bound, ensure_ascii=False, indent=2), encoding="utf-8")
    return output, str(bound["approved_hash"])


def _output_dirs() -> set[Path]:
    return {path.resolve() for path in Path("output").glob("*") if path.is_dir()}


def _new_output_dir(before: set[Path]) -> Path:
    after = _output_dirs()
    created = [path for path in after if path not in before]
    if len(created) != 1:
        raise RuntimeError(f"Control production expected exactly one new output directory, found {len(created)}")
    return created[0]


def write_sibling_short_plan(output_dir: Path, request: dict[str, Any]) -> Path | None:
    if request.get("approval_scope") != "long_plus_sibling_shorts":
        return None
    plan_path = Path(output_dir) / "plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    sections = plan.get("sections") if isinstance(plan, dict) else None
    if not isinstance(sections, list):
        raise RuntimeError("Long plan cannot produce sibling Short semantic jobs")
    jobs = select_sibling_jobs(
        str(item.get("key_point") or "").strip()
        for item in sections
        if isinstance(item, dict) and str(item.get("key_point") or "").strip()
    )
    minimum = int((request.get("sibling_shorts") or {}).get("minimum", 2))
    maximum = int((request.get("sibling_shorts") or {}).get("maximum", 3))
    if minimum < 2 or maximum > 3 or minimum > maximum:
        raise RuntimeError("Sibling Short quota must stay within 2–3")
    if len(jobs) < minimum:
        raise RuntimeError("Long episode does not contain enough independent jobs for approved sibling Shorts")
    selected = list(jobs[:maximum])
    document = {
        "schema_version": 1,
        "source_request_id": request.get("request_id"),
        "source_request_sha256": request.get("request_sha256"),
        "source_topic": request.get("approved_topic"),
        "short_count": len(selected),
        "semantic_jobs": [
            {
                "index": index,
                "semantic_job": job,
                "status": "planned_not_dispatched",
                "production_dispatch_authorized": False,
            }
            for index, job in enumerate(selected, 1)
        ],
        "publish_spacing_hours": DEFAULT_SIBLING_SPACING_HOURS,
        "automatic_production_started": False,
        "youtube_publish_mode": "manual_in_youtube_studio",
    }
    path = Path(output_dir) / "sibling-short-plan.json"
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _set_runtime_env(values: dict[str, str]) -> dict[str, str | None]:
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    return previous


def _restore_runtime_env(previous: dict[str, str | None]) -> None:
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def execute_control_request(request: dict[str, Any], *, runtime_dir: Path) -> Path:
    validate_control_request(request, str(request.get("request_sha256") or ""))
    runtime_dir = Path(runtime_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    request_copy = runtime_dir / "control-request.json"
    request_copy.write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
    brief_path, brief_hash = materialize_approved_brief(request, runtime_dir / "approved-brief.json")
    request_file = runtime_dir / "isco-request.json"
    request_file.write_text(
        json.dumps(
            {
                "topic": request["approved_topic"],
                "format": "moment" if request["kind"] == "short" else request["format"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    previous_env = _set_runtime_env(
        {
            "ISCO_APPROVED_BRIEF_PATH": str(brief_path.resolve()),
            "ISCO_APPROVED_BRIEF_SHA256": brief_hash,
            "REQUEST_FILE": str(request_file.resolve()),
            "ISCO_CONTROL_REQUEST_ID": str(request.get("request_id") or ""),
        }
    )
    runtime_request = dict(request)
    runtime_request["production_dispatch_authorized"] = True
    original_gold = production.run_gold_enforce_phase4
    short_pre: dict[str, Any] | None = None
    before = _output_dirs()

    def controlled_gold(**kwargs):
        nonlocal short_pre
        if request["kind"] == "short":
            short_pre = prepare_short_render(Path(kwargs["output_dir"]), runtime_request)
        result = original_gold(**kwargs)
        if request["kind"] == "short":
            assert short_pre is not None
            finalize_short_quality(Path(kwargs["output_dir"]), runtime_request, short_pre)
        return result

    production.run_gold_enforce_phase4 = controlled_gold
    try:
        production.main()
        output = _new_output_dir(before)
    finally:
        production.run_gold_enforce_phase4 = original_gold
        _restore_runtime_env(previous_env)

    if request["kind"] == "long":
        write_sibling_short_plan(output, request)
    return output


def execute_bundle(parent_request: dict[str, Any], *, runtime_root: Path) -> Path:
    parent_output = execute_control_request(parent_request, runtime_dir=Path(runtime_root) / "parent")
    if parent_request.get("approval_scope") != "long_plus_sibling_shorts":
        return parent_output

    sibling_plan = parent_output / "sibling-short-plan.json"
    if not sibling_plan.is_file():
        raise RuntimeError("Approved long+Shorts request completed without sibling-short-plan.json")

    def execute_child(child_request: dict[str, Any]) -> Path:
        return execute_control_request(
            child_request,
            runtime_dir=Path(runtime_root) / str(child_request.get("request_id") or "child"),
        )

    completed = orchestrate_sibling_shorts(parent_request, sibling_plan, execute_short=execute_child)
    staged = stage_sibling_assets(parent_output, completed)
    (parent_output / "sibling-short-results.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "parent_request_id": parent_request.get("request_id"),
                "short_count": len(staged),
                "shorts": staged,
                "partial_delivery_allowed": False,
                "youtube_publish_mode": "manual_in_youtube_studio",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    write_delivery_manifest(
        parent_output,
        repository=(os.environ.get("GITHUB_REPOSITORY") or "mymusa79-tech/Isco-Video-Runner").strip(),
        release_tag=None,
        request=parent_request,
        short_assets=staged,
    )
    return parent_output


def main() -> None:
    if (os.environ.get("CONTROL_PLANE_PRODUCTION_ENABLED") or "false").strip().lower() != "true":
        raise RuntimeError("Control-plane production is locked; no Production Run is authorized")
    request_path = Path(os.environ.get("ISCO_CONTROL_REQUEST_PATH") or "")
    expected = (os.environ.get("ISCO_CONTROL_REQUEST_SHA256") or "").strip()
    if not str(request_path) or not expected:
        raise RuntimeError("Control production requires request path and exact request hash")
    request = load_control_request(request_path, expected)
    runtime_root = Path(os.environ.get("RUNNER_TEMP") or ".") / "isco-control-runtime" / str(request.get("request_id") or "request")
    execute_bundle(request, runtime_root=runtime_root)


if __name__ == "__main__":
    main()

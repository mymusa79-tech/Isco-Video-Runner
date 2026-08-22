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


def _canonical_request_hash(request: dict[str, Any]) -> str:
    subject = {key: value for key, value in request.items() if key != "request_sha256"}
    encoded = json.dumps(subject, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_control_request(path: Path, expected_sha256: str) -> dict[str, Any]:
    try:
        request = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError("Control request is missing or invalid JSON") from exc
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
    return request


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


def _latest_output_dir() -> Path | None:
    roots = sorted(Path("output").glob("*"), key=lambda path: path.stat().st_mtime, reverse=True)
    return roots[0] if roots else None


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
    if len(jobs) < minimum:
        raise RuntimeError("Long episode does not contain enough independent jobs for approved sibling Shorts")
    selected = list(jobs[:maximum])
    document = {
        "schema_version": 1,
        "source_request_id": request.get("request_id"),
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
    }
    path = Path(output_dir) / "sibling-short-plan.json"
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main() -> None:
    if (os.environ.get("CONTROL_PLANE_PRODUCTION_ENABLED") or "false").strip().lower() != "true":
        raise RuntimeError("Control-plane production is locked; no Production Run is authorized")
    request_path = Path(os.environ.get("ISCO_CONTROL_REQUEST_PATH") or "")
    expected = (os.environ.get("ISCO_CONTROL_REQUEST_SHA256") or "").strip()
    if not str(request_path) or not expected:
        raise RuntimeError("Control production requires request path and exact request hash")
    request = load_control_request(request_path, expected)

    runtime_dir = Path(os.environ.get("RUNNER_TEMP") or ".") / "isco-control-runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
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
    os.environ["ISCO_APPROVED_BRIEF_PATH"] = str(brief_path.resolve())
    os.environ["ISCO_APPROVED_BRIEF_SHA256"] = brief_hash
    os.environ["REQUEST_FILE"] = str(request_file.resolve())
    os.environ["ISCO_CONTROL_REQUEST_ID"] = str(request.get("request_id") or "")

    runtime_request = dict(request)
    runtime_request["production_dispatch_authorized"] = True
    original_gold = production.run_gold_enforce_phase4
    short_pre: dict[str, Any] | None = None

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
    finally:
        production.run_gold_enforce_phase4 = original_gold

    output = _latest_output_dir()
    if output is None:
        raise RuntimeError("Control production completed without an output directory")
    if request["kind"] == "long":
        write_sibling_short_plan(output, request)


if __name__ == "__main__":
    main()

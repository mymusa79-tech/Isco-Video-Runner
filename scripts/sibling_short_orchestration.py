from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Callable, Iterable

from isco_video_agent.short_topic_gate import evaluate_short_topic

SCHEMA_VERSION = 1
MIN_SIBLING_SHORTS = 2
MAX_SIBLING_SHORTS = 3


def _canonical_hash(document: dict[str, Any]) -> str:
    payload = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _score01(value: object) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, parsed))


def _single_action(candidate: dict[str, Any]) -> str:
    pillar = str(candidate.get("pillar") or "understand").strip().lower()
    if pillar == "rise":
        return "اختر خطوة واحدة صغيرة قابلة للتنفيذ وابدأ بها اليوم"
    if pillar == "see":
        return "أعد النظر في موقف واحد اليوم من الزاوية الجديدة"
    return "لاحظ موقفًا واحدًا اليوم واسأل ما الذي يحرّكه فعلًا"


def _short_admission_from_parent(parent_request: dict[str, Any]) -> dict[str, Any]:
    candidate = parent_request.get("candidate")
    if not isinstance(candidate, dict):
        raise RuntimeError("Sibling Shorts require the approved parent candidate evidence")

    hook = _score01(candidate.get("hook_potential"))
    retention = _score01(candidate.get("retention_potential"))
    emotional = _score01(candidate.get("emotional_pull"))
    audience = _score01(candidate.get("audience_fit"))
    packaging = _score01(candidate.get("title_thumbnail_potential"))
    feasibility = _score01(candidate.get("production_feasibility"))
    evidence_quality = _score01(candidate.get("evidence_quality"))

    short_fit = (
        hook * 0.23
        + retention * 0.22
        + emotional * 0.15
        + audience * 0.15
        + packaging * 0.10
        + feasibility * 0.10
        + evidence_quality * 0.05
    )
    admission = {
        "knowledge_gap_score": round(max(hook, packaging) * 10.0, 2),
        "reframe_score": round(max(emotional, audience) * 10.0, 2),
        "immediate_action_score": round(feasibility * 10.0, 2),
        "short_fit_score": round(short_fit * 10.0, 2),
        "single_action_contract": _single_action(candidate),
        "evidence_source": "approved_parent_candidate_metrics",
    }
    result = evaluate_short_topic(admission)
    if result.get("decision") != "pass":
        reasons = ", ".join(str(x) for x in result.get("reasons", []))
        raise RuntimeError(f"Approved long-form topic is not strong enough for sibling Shorts: {reasons}")
    return admission


def _semantic_jobs(plan: dict[str, Any]) -> list[str]:
    raw = plan.get("semantic_jobs")
    if not isinstance(raw, list):
        raise RuntimeError("Sibling Short plan has no semantic_jobs list")
    jobs: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise RuntimeError("Sibling Short semantic job is malformed")
        if item.get("production_dispatch_authorized") is not False:
            raise RuntimeError("Sibling Short plan must remain non-dispatching before orchestration")
        if item.get("status") != "planned_not_dispatched":
            raise RuntimeError("Sibling Short plan contains an unexpected status")
        job = " ".join(str(item.get("semantic_job") or "").strip().split())
        key = job.casefold()
        if not job or key in seen:
            raise RuntimeError("Sibling Short jobs must be non-empty and distinct")
        seen.add(key)
        jobs.append(job)
    if not MIN_SIBLING_SHORTS <= len(jobs) <= MAX_SIBLING_SHORTS:
        raise RuntimeError("Sibling Short plan must contain 2–3 distinct semantic jobs")
    return jobs


def build_sibling_requests(parent_request: dict[str, Any], sibling_plan: dict[str, Any]) -> list[dict[str, Any]]:
    if parent_request.get("approved_by_user") is not True:
        raise RuntimeError("Sibling Shorts require explicit parent user approval")
    if parent_request.get("kind") != "long":
        raise RuntimeError("Sibling Shorts require a long-form parent request")
    if parent_request.get("approval_scope") != "long_plus_sibling_shorts":
        raise RuntimeError("Parent request did not approve sibling Shorts")
    if parent_request.get("production_dispatch_authorized") is not False:
        raise RuntimeError("Stored parent request must remain non-dispatching")
    parent_id = str(parent_request.get("request_id") or "").strip()
    parent_sha = str(parent_request.get("request_sha256") or "").strip()
    parent_topic = str(parent_request.get("approved_topic") or "").strip()
    if not parent_id or not parent_sha or not parent_topic:
        raise RuntimeError("Parent control request provenance is incomplete")
    if sibling_plan.get("source_request_id") != parent_id:
        raise RuntimeError("Sibling Short plan does not belong to the approved parent request")
    if str(sibling_plan.get("source_topic") or "").strip() != parent_topic:
        raise RuntimeError("Sibling Short plan source topic does not match the approved parent topic")
    if sibling_plan.get("automatic_production_started") is not False:
        raise RuntimeError("Sibling Short plan already claims automatic production started")

    jobs = _semantic_jobs(sibling_plan)
    admission = _short_admission_from_parent(parent_request)
    candidate = parent_request.get("candidate")
    assert isinstance(candidate, dict)

    requests: list[dict[str, Any]] = []
    for index, job in enumerate(jobs, 1):
        request: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "request_id": f"{parent_id}-s{index}",
            "source": "telegram_editorial_control_panel",
            "kind": "short",
            "approval_scope": "short_sibling",
            "approved_by_user": True,
            "approval_inherited_from_parent_bundle": True,
            "approved_at": parent_request.get("approved_at"),
            "approved_topic": job,
            "format": "moment",
            "weekly_option_id": f"{parent_request.get('weekly_option_id') or parent_id}:s{index}",
            "content_boundaries": list(parent_request.get("content_boundaries") or []),
            "approved_research_pack": [],
            "candidate": {
                "title": job,
                "pillar": candidate.get("pillar"),
                "format_hint": "moment",
                "source_long_topic": parent_topic,
                "source_candidate_control_score": candidate.get("control_score"),
            },
            "short_admission": dict(admission),
            "parent_control_request_id": parent_id,
            "parent_control_request_sha256": parent_sha,
            "source_long_topic": parent_topic,
            "source_semantic_job": job,
            "sibling_index": index,
            "sibling_count": len(jobs),
            "production_dispatch_authorized": False,
            "status": "approved_waiting_production_activation",
            "youtube_publish_mode": "manual_in_youtube_studio",
        }
        request["request_sha256"] = _canonical_hash(request)
        requests.append(request)
    return requests


def _read_object(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Invalid JSON artifact: {Path(path).name}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"Artifact must be a JSON object: {Path(path).name}")
    return data


def validate_completed_short(output_dir: Path, request: dict[str, Any]) -> dict[str, Any]:
    root = Path(output_dir)
    final = root / "final.mp4"
    if not final.is_file() or final.stat().st_size <= 1024:
        raise RuntimeError("Sibling Short output has no usable final.mp4")
    quality = _read_object(root / "quality-final.json")
    intelligence = _read_object(root / "short-intelligence.json")
    gold = _read_object(root / "gold-enforce-report.json")
    rights = _read_object(root / "rights-manifest.json")
    plan = _read_object(root / "plan.json")

    if quality.get("format") != "moment" or quality.get("duration_ok") is not True:
        raise RuntimeError("Sibling Short failed its final moment-duration contract")
    if intelligence.get("delivery_allowed") is not True:
        raise RuntimeError("Sibling Short intelligence did not allow delivery")
    if intelligence.get("request_id") != request.get("request_id"):
        raise RuntimeError("Sibling Short intelligence request provenance mismatch")
    if not (
        gold.get("phase") == "4"
        and gold.get("mode") == "enforce"
        and isinstance(gold.get("gold"), dict)
        and gold["gold"].get("accepted") is True
        and isinstance(gold.get("same_render"), dict)
        and gold["same_render"].get("artifact_divergence") is False
    ):
        raise RuntimeError("Sibling Short Gold contract is not accepted")
    if not rights:
        raise RuntimeError("Sibling Short rights manifest is empty")
    if str(plan.get("topic") or "").strip() != str(request.get("approved_topic") or "").strip():
        raise RuntimeError("Sibling Short plan topic does not match its approved semantic job")

    return {
        "request_id": request.get("request_id"),
        "request_sha256": request.get("request_sha256"),
        "semantic_job": request.get("source_semantic_job"),
        "output_dir": str(root),
        "duration_seconds": quality.get("duration_seconds") or quality.get("video_stream_duration"),
        "final_video": str(final),
        "quality_report": str(root / "quality-final.json"),
        "short_intelligence": str(root / "short-intelligence.json"),
        "gold_report": str(root / "gold-enforce-report.json"),
        "rights_manifest": str(root / "rights-manifest.json"),
        "plan": str(root / "plan.json"),
        "delivery_allowed": True,
    }


def orchestrate_sibling_shorts(
    parent_request: dict[str, Any],
    sibling_plan_path: Path,
    *,
    execute_short: Callable[[dict[str, Any]], Path],
) -> list[dict[str, Any]]:
    plan = _read_object(sibling_plan_path)
    requests = build_sibling_requests(parent_request, plan)
    completed: list[dict[str, Any]] = []
    for request in requests:
        output = Path(execute_short(request))
        completed.append(validate_completed_short(output, request))
    if len(completed) != len(requests):
        raise RuntimeError("Sibling Short orchestration ended with a partial set")
    return completed


def stage_sibling_assets(parent_output_dir: Path, completed: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    root = Path(parent_output_dir)
    staged: list[dict[str, Any]] = []
    items = list(completed)
    if not MIN_SIBLING_SHORTS <= len(items) <= MAX_SIBLING_SHORTS:
        raise RuntimeError("Unified delivery requires 2–3 completed sibling Shorts")
    for index, item in enumerate(items, 1):
        prefix = f"short-{index:02d}"
        mappings = (
            ("final_video", f"{prefix}.mp4", "video"),
            ("short_intelligence", f"{prefix}-intelligence.json", "short_intelligence"),
            ("gold_report", f"{prefix}-gold.json", "gold_report"),
            ("rights_manifest", f"{prefix}-rights.json", "rights_manifest"),
            ("quality_report", f"{prefix}-quality.json", "quality_report"),
            ("plan", f"{prefix}-plan.json", "plan"),
        )
        record = {
            "slot": f"S{index}",
            "semantic_job": item.get("semantic_job"),
            "request_id": item.get("request_id"),
            "request_sha256": item.get("request_sha256"),
            "duration_seconds": item.get("duration_seconds"),
            "delivery_allowed": True,
        }
        for source_key, filename, record_key in mappings:
            source = Path(str(item.get(source_key) or ""))
            if not source.is_file():
                raise RuntimeError(f"Completed sibling Short is missing {source_key}")
            destination = root / filename
            shutil.copy2(source, destination)
            record[record_key] = filename
        staged.append(record)
    return staged

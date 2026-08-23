from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from isco_video_agent.brief_approval_binding import verify_brief_approval
from isco_video_agent.short_planner import DEFAULT_SIBLING_SPACING_HOURS, select_sibling_jobs

from scripts.packaging_delivery_contract import validate_packaging_delivery
from scripts.sibling_short_orchestration import (
    _short_admission_from_parent,
    stage_sibling_assets,
    validate_completed_short,
)
from scripts.source_derived_short_planner import build_source_short_blueprint
from scripts.unified_delivery import write_delivery_manifest


SOURCE = "canonical_v4_approved_brief"
MIN_SHORTS = 2
MAX_SHORTS = 3


def _read_object(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Canonical V4 bundle missing or invalid artifact: {Path(path).name}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"Canonical V4 bundle artifact must be an object: {Path(path).name}")
    return data


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_hash(document: dict[str, Any]) -> str:
    subject = {key: value for key, value in document.items() if key != "request_sha256"}
    payload = json.dumps(subject, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _score01(review: dict[str, Any], field: str) -> float:
    value = review.get(field)
    if isinstance(value, bool):
        raise RuntimeError(f"Canonical V4 evidence score is invalid: {field}")
    try:
        score = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Canonical V4 evidence score is missing: {field}") from exc
    if not 0.0 <= score <= 1.0:
        raise RuntimeError(f"Canonical V4 evidence score escaped 0..1: {field}")
    return score


def _accepted_gold(gold: dict[str, Any]) -> bool:
    return bool(
        gold.get("phase") == "4"
        and gold.get("mode") == "enforce"
        and isinstance(gold.get("gold"), dict)
        and gold["gold"].get("accepted") is True
        and isinstance(gold.get("same_render"), dict)
        and gold["same_render"].get("artifact_divergence") is False
    )


def _candidate_from_real_long_evidence(root: Path, plan: dict[str, Any]) -> dict[str, Any]:
    critic = _read_object(root / "final-critic.json")
    review = critic.get("model_review")
    if critic.get("status") != "pass" or not isinstance(review, dict) or review.get("status") != "pass":
        raise RuntimeError("Canonical sibling Shorts require a passing long-form Final Critic")
    if critic.get("hard_blocks") or review.get("critical_issues"):
        raise RuntimeError("Canonical sibling Shorts cannot inherit a blocked long-form Final Critic")

    quality = _read_object(root / "quality-final.json")
    factual = _read_object(root / "factuality-audit.json")
    precheck = _read_object(root / "quality-precheck.json")
    gold = _read_object(root / "gold-enforce-report.json")
    if factual.get("status") != "pass":
        raise RuntimeError("Canonical sibling Shorts require factuality pass on the source episode")
    if int(precheck.get("research_source_count") or 0) < 2:
        raise RuntimeError("Canonical sibling Shorts require at least two approved source-episode research sources")
    if not _accepted_gold(gold):
        raise RuntimeError("Canonical sibling Shorts require accepted source-episode Gold")
    if quality.get("duration_ok") is not True or quality.get("audio_ok") is not True:
        raise RuntimeError("Canonical sibling Shorts require passing source-episode media quality")

    packaging = validate_packaging_delivery(root)
    if len(packaging) < 3:
        raise RuntimeError("Canonical sibling Shorts require complete A/B/C long-form packaging")

    evidence = {
        "hook_potential": _score01(review, "opening_strength"),
        "retention_potential": _score01(review, "narrative_progression"),
        "emotional_pull": _score01(review, "human_feel"),
        "audience_fit": _score01(review, "cultural_fit"),
        "title_thumbnail_potential": 1.0,
        "production_feasibility": 1.0,
        "evidence_quality": 1.0,
    }
    control_score = round(sum(evidence.values()) / len(evidence), 6)
    return {
        "title": str(plan.get("topic") or "").strip(),
        "pillar": str(plan.get("pillar") or "understand").strip() or "understand",
        "format_hint": str(plan.get("format") or "film"),
        **evidence,
        "control_score": control_score,
        "evidence_map": {
            "hook_potential": "final-critic.model_review.opening_strength",
            "retention_potential": "final-critic.model_review.narrative_progression",
            "emotional_pull": "final-critic.model_review.human_feel",
            "audience_fit": "final-critic.model_review.cultural_fit",
            "title_thumbnail_potential": "packaging_delivery_contract=A/B/C complete",
            "production_feasibility": "gold.accepted + quality.duration_ok + quality.audio_ok",
            "evidence_quality": "factuality.status=pass + research_source_count>=2",
        },
        "score_origin": "post_gold_source_episode_evidence_only",
    }


def _approved_brief() -> tuple[dict[str, Any], str]:
    raw_path = str(os.environ.get("ISCO_APPROVED_BRIEF_PATH") or "").strip()
    approved_hash = str(os.environ.get("ISCO_APPROVED_BRIEF_SHA256") or "").strip()
    if not raw_path or not approved_hash:
        raise RuntimeError("Canonical V4 bundle requires the exact approved brief path and hash")
    brief = _read_object(Path(raw_path))
    if brief.get("approved_by_user") is not True:
        raise RuntimeError("Canonical V4 bundle requires explicit user-approved source brief")
    verified = verify_brief_approval(brief, approved_hash)
    return brief, verified


def _source_excerpt(source_plan: dict[str, Any], job: str) -> dict[str, Any]:
    sections = source_plan.get("sections")
    if not isinstance(sections, list):
        raise RuntimeError("Canonical V4 source plan has no sections")
    wanted = " ".join(job.strip().split()).casefold()
    matches = [
        section
        for section in sections
        if isinstance(section, dict)
        and " ".join(str(section.get("key_point") or "").strip().split()).casefold() == wanted
    ]
    if len(matches) != 1:
        raise RuntimeError("Canonical V4 sibling job must map to exactly one source section")
    section = matches[0]
    narration = " ".join(str(section.get("narration") or "").strip().split())
    visual_query = " ".join(str(section.get("visual_query") or "").strip().split())
    key_point = " ".join(str(section.get("key_point") or "").strip().split())
    if not narration or not visual_query or not key_point:
        raise RuntimeError("Canonical V4 source section lacks narration, visual query, or key point")
    return {
        "source_section_id": str(section.get("id") or "unknown").strip() or "unknown",
        "source_key_point": key_point,
        "source_on_screen_text": " ".join(str(section.get("on_screen_text") or "").strip().split()),
        "source_visual_query": visual_query,
        "source_emotion": str(section.get("emotion") or "reflective").strip() or "reflective",
        "source_narration": narration,
        "source_narration_sha256": _sha256_text(narration),
    }


def build_parent_request(root: Path) -> dict[str, Any]:
    root = Path(root)
    plan = _read_object(root / "plan.json")
    if str(plan.get("format") or "") == "moment":
        raise RuntimeError("Canonical long+Shorts bundle cannot use a moment as the parent")
    brief, brief_sha = _approved_brief()
    topic = str(plan.get("topic") or "").strip()
    if topic != str(brief.get("approved_topic") or "").strip():
        raise RuntimeError("Canonical V4 source plan topic escaped the approved brief")
    production_id = str(os.environ.get("ISCO_PRODUCTION_ID") or root.name).strip().replace(":", "-")
    request: dict[str, Any] = {
        "schema_version": 1,
        "request_id": f"canonical-{production_id}",
        "source": SOURCE,
        "kind": "long",
        "approval_scope": "long_plus_sibling_shorts",
        "approved_by_user": True,
        "approval_inherited_from_approved_brief": True,
        "approved_at": brief.get("approved_at"),
        "approved_topic": topic,
        "format": str(plan.get("format") or brief.get("format") or "film"),
        "weekly_option_id": brief.get("weekly_option_id"),
        "content_boundaries": list(brief.get("content_boundaries") or []),
        "approved_research_pack": list(brief.get("research_pack") or []),
        "parent_approved_brief_sha256": brief_sha,
        "source_long_plan_sha256": _sha256_file(root / "plan.json"),
        "source_long_final_sha256": _sha256_file(root / "final.mp4"),
        "candidate": _candidate_from_real_long_evidence(root, plan),
        "sibling_shorts": {"minimum": MIN_SHORTS, "maximum": MAX_SHORTS},
        "production_dispatch_authorized": False,
        "status": "approved_waiting_production_activation",
        "youtube_publish_mode": "manual_in_youtube_studio",
        "publication_performed": False,
    }
    request["request_sha256"] = _canonical_hash(request)
    return request


def build_sibling_plan(root: Path, parent: dict[str, Any]) -> dict[str, Any]:
    source_plan = _read_object(Path(root) / "plan.json")
    sections = source_plan.get("sections")
    if not isinstance(sections, list):
        raise RuntimeError("Canonical V4 long plan cannot produce sibling semantic jobs")
    jobs = select_sibling_jobs(
        str(section.get("key_point") or "").strip()
        for section in sections
        if isinstance(section, dict) and str(section.get("key_point") or "").strip()
    )
    selected = list(jobs[:MAX_SHORTS])
    if len(selected) < MIN_SHORTS:
        raise RuntimeError("Canonical V4 long episode has fewer than two distinct sibling Short jobs")
    return {
        "schema_version": 1,
        "source_request_id": parent["request_id"],
        "source_request_sha256": parent["request_sha256"],
        "source_production_plan_sha256": _sha256_file(Path(root) / "plan.json"),
        "source_topic": parent["approved_topic"],
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


def build_child_requests(parent: dict[str, Any], sibling_plan: dict[str, Any], source_plan: dict[str, Any]) -> list[dict[str, Any]]:
    admission = _short_admission_from_parent(parent)
    jobs = [str(item.get("semantic_job") or "").strip() for item in sibling_plan.get("semantic_jobs") or []]
    if not MIN_SHORTS <= len(jobs) <= MAX_SHORTS or len({job.casefold() for job in jobs}) != len(jobs):
        raise RuntimeError("Canonical V4 sibling plan must contain 2–3 distinct jobs")
    sibling_plan_sha = _canonical_hash(sibling_plan)
    candidate = parent["candidate"]
    requests: list[dict[str, Any]] = []
    for index, job in enumerate(jobs, 1):
        excerpt = _source_excerpt(source_plan, job)
        request: dict[str, Any] = {
            "schema_version": 1,
            "request_id": f"{parent['request_id']}-s{index}",
            "source": SOURCE,
            "kind": "short",
            "approval_scope": "short_sibling",
            "approved_by_user": True,
            "approval_inherited_from_approved_brief": True,
            "approval_inherited_from_parent_bundle": True,
            "approved_at": parent.get("approved_at"),
            "approved_topic": job,
            "format": "moment",
            "weekly_option_id": f"{parent.get('weekly_option_id') or parent['request_id']}:s{index}",
            "content_boundaries": list(parent.get("content_boundaries") or []),
            "approved_research_pack": [],
            "candidate": {
                "title": job,
                "pillar": candidate.get("pillar"),
                "format_hint": "moment",
                "source_long_topic": parent["approved_topic"],
                "source_candidate_control_score": candidate.get("control_score"),
            },
            "short_admission": dict(admission),
            "parent_control_request_id": parent["request_id"],
            "parent_control_request_sha256": parent["request_sha256"],
            "parent_approved_brief_sha256": parent["parent_approved_brief_sha256"],
            "source_production_plan_sha256": sibling_plan["source_production_plan_sha256"],
            "source_sibling_plan_sha256": sibling_plan_sha,
            "source_long_topic": parent["approved_topic"],
            "source_semantic_job": job,
            "source_episode_excerpt": excerpt,
            "sibling_index": index,
            "sibling_count": len(jobs),
            "production_dispatch_authorized": False,
            "status": "approved_waiting_production_activation",
            "youtube_publish_mode": "manual_in_youtube_studio",
        }
        request["source_short_plan"] = build_source_short_blueprint(request)
        request["request_sha256"] = _canonical_hash(request)
        requests.append(request)
    return requests


def _execute_child(request: dict[str, Any], *, runtime_root: Path) -> Path:
    request_dir = Path(runtime_root) / str(request["request_id"])
    request_dir.mkdir(parents=True, exist_ok=True)
    request_path = request_dir / "request.json"
    result_path = request_dir / "result.json"
    request_path.write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
    subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("canonical_v4_short_child.py").resolve()),
            "--request",
            str(request_path.resolve()),
            "--sha256",
            str(request["request_sha256"]),
            "--result",
            str(result_path.resolve()),
        ],
        check=True,
        env=os.environ.copy(),
    )
    result = _read_object(result_path)
    output = Path(str(result.get("output_dir") or ""))
    if not output.is_dir():
        raise RuntimeError("Canonical V4 sibling child returned no output directory")
    return output


def build_canonical_v4_bundle(output_dir: Path) -> Path | None:
    root = Path(output_dir)
    plan = _read_object(root / "plan.json")
    if str(plan.get("format") or "") == "moment":
        return None
    if str(os.environ.get("ISCO_CONTROL_REQUEST_ID") or "").strip():
        return None
    existing = root / "delivery-manifest.json"
    if existing.is_file():
        return existing

    parent = build_parent_request(root)
    sibling_plan = build_sibling_plan(root, parent)
    _short_admission_from_parent(parent)  # fail closed before any child subprocess starts
    source_plan = _read_object(root / "plan.json")
    children = build_child_requests(parent, sibling_plan, source_plan)

    (root / "canonical-bundle-request.json").write_text(
        json.dumps(parent, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    sibling_plan_path = root / "sibling-short-plan.json"
    sibling_plan_path.write_text(json.dumps(sibling_plan, ensure_ascii=False, indent=2), encoding="utf-8")

    runtime_root = Path(os.environ.get("RUNNER_TEMP") or ".") / "isco-canonical-v4-bundle" / parent["request_id"]
    completed: list[dict[str, Any]] = []
    for request in children:
        output = _execute_child(request, runtime_root=runtime_root)
        completed.append(validate_completed_short(output, request))
    if len(completed) != len(children):
        raise RuntimeError("Canonical V4 bundle ended with a partial sibling Short set")

    staged = stage_sibling_assets(root, completed)
    (root / "sibling-short-results.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "parent_request_id": parent["request_id"],
                "parent_request_sha256": parent["request_sha256"],
                "short_count": len(staged),
                "shorts": staged,
                "execution_mode": "sequential_isolated_subprocesses",
                "short_source_mode": "exact_long_episode_sections",
                "partial_delivery_allowed": False,
                "youtube_publish_mode": "manual_in_youtube_studio",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return write_delivery_manifest(
        root,
        repository=(os.environ.get("GITHUB_REPOSITORY") or "mymusa79-tech/Isco-Video-Runner").strip(),
        release_tag=None,
        request=parent,
        short_assets=staged,
    )

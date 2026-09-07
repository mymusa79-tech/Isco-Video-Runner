from __future__ import annotations

import hashlib
import json
import math
import subprocess
from pathlib import Path
from typing import Any

from isco_video_agent.media.ffmpeg import concat_video, duration
from isco_video_agent.security import secret_free_subprocess_env


SCHEMA_VERSION = 1
PROFILE = "source_derived_parent_visual_capsule_v1"
MAX_INHERITED_SHOTS = 4
MIN_INHERITED_SHOT_SECONDS = 1.0


class SourceDerivedVisualCapsuleError(RuntimeError):
    pass


def _clean(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_hash(document: dict[str, Any]) -> str:
    subject = {key: value for key, value in document.items() if key != "capsule_sha256"}
    payload = json.dumps(subject, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _read_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise SourceDerivedVisualCapsuleError(f"source_visual_capsule_invalid_json:{Path(path).name}") from exc
    if not isinstance(payload, dict):
        raise SourceDerivedVisualCapsuleError(f"source_visual_capsule_wrong_shape:{Path(path).name}")
    return payload


def _seconds(value: object, field: str) -> float:
    if isinstance(value, bool):
        raise SourceDerivedVisualCapsuleError(f"source_visual_capsule_invalid_{field}")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise SourceDerivedVisualCapsuleError(f"source_visual_capsule_invalid_{field}") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise SourceDerivedVisualCapsuleError(f"source_visual_capsule_invalid_{field}")
    return parsed


def validate_capsule_document(capsule: object) -> dict[str, Any]:
    if not isinstance(capsule, dict):
        raise SourceDerivedVisualCapsuleError("source_visual_capsule_missing")
    if capsule.get("schema_version") != SCHEMA_VERSION or capsule.get("profile") != PROFILE:
        raise SourceDerivedVisualCapsuleError("source_visual_capsule_schema_invalid")
    stored = _clean(capsule.get("capsule_sha256"))
    if len(stored) != 64 or stored != _canonical_hash(capsule):
        raise SourceDerivedVisualCapsuleError("source_visual_capsule_hash_mismatch")
    section_id = _clean(capsule.get("source_section_id"))
    shots = capsule.get("shots")
    policy = capsule.get("inheritance_policy")
    if not section_id or not isinstance(shots, list) or not shots:
        raise SourceDerivedVisualCapsuleError("source_visual_capsule_section_or_shots_missing")
    if not isinstance(policy, dict) or policy.get("actual_parent_video_required") is not True:
        raise SourceDerivedVisualCapsuleError("source_visual_capsule_video_inheritance_not_required")
    if policy.get("textual_visual_query_is_not_video_authority") is not True:
        raise SourceDerivedVisualCapsuleError("source_visual_capsule_query_authority_regression")
    if policy.get("new_stock_search_allowed") is not False or policy.get("new_vision_review_allowed") is not False:
        raise SourceDerivedVisualCapsuleError("source_visual_capsule_provider_reuse_policy_regression")
    return dict(capsule)


def _section_from_timeline(timeline: dict[str, Any], section_id: str) -> dict[str, Any]:
    sections = timeline.get("sections")
    if not isinstance(sections, list):
        raise SourceDerivedVisualCapsuleError("source_visual_timeline_sections_missing")
    matches = [item for item in sections if isinstance(item, dict) and _clean(item.get("section_id")) == section_id]
    if len(matches) != 1:
        raise SourceDerivedVisualCapsuleError("source_visual_section_not_unique")
    return matches[0]


def _section_shots(timeline: dict[str, Any], section_id: str) -> list[dict[str, Any]]:
    flat = timeline.get("final_cut_visuals")
    if not isinstance(flat, list):
        raise SourceDerivedVisualCapsuleError("source_visual_timeline_final_cut_missing")
    shots = [item for item in flat if isinstance(item, dict) and _clean(item.get("section_id")) == section_id]
    if not shots:
        raise SourceDerivedVisualCapsuleError("source_visual_section_has_no_final_cut_shots")
    cursor: float | None = None
    normalized: list[dict[str, Any]] = []
    for shot in shots:
        start = _seconds(shot.get("start_seconds"), "shot_start")
        end = _seconds(shot.get("end_seconds"), "shot_end")
        if end <= start:
            raise SourceDerivedVisualCapsuleError("source_visual_shot_non_positive")
        if cursor is not None and abs(start - cursor) > 0.02:
            raise SourceDerivedVisualCapsuleError("source_visual_shots_not_contiguous")
        selected = shot.get("selected_asset")
        if not isinstance(selected, dict) or not _clean(selected.get("provider")) or selected.get("asset_id") in (None, ""):
            raise SourceDerivedVisualCapsuleError("source_visual_shot_provenance_missing")
        audit_ref = shot.get("final_cut_audit_reference")
        if not isinstance(audit_ref, dict):
            raise SourceDerivedVisualCapsuleError("source_visual_shot_audit_reference_missing")
        normalized.append(
            {
                "shot_id": _clean(shot.get("shot_id")),
                "start_seconds": round(start, 3),
                "end_seconds": round(end, 3),
                "continuity_role": _clean(shot.get("continuity_role")),
                "visual_job": _clean(shot.get("visual_job")),
                "story_job": _clean(shot.get("story_job")),
                "cut_reason": _clean(shot.get("cut_reason")),
                "selected_asset": {
                    "candidate_ref": selected.get("candidate_ref"),
                    "provider": _clean(selected.get("provider")).lower(),
                    "asset_id": selected.get("asset_id"),
                    "source_url": selected.get("source_url"),
                },
                "final_cut_audit_reference": dict(audit_ref),
                "rights_reference": shot.get("rights_reference"),
            }
        )
        cursor = end
    return normalized


def build_parent_visual_capsule(parent_output_dir: Path, section_id: object) -> dict[str, Any]:
    root = Path(parent_output_dir)
    section_key = _clean(section_id)
    if not section_key:
        raise SourceDerivedVisualCapsuleError("source_visual_section_id_missing")
    picture = root / "picture.mp4"
    final = root / "final.mp4"
    timeline_path = root / "visual-timeline.json"
    rights_path = root / "rights-manifest.json"
    for path in (picture, final, timeline_path, rights_path):
        if not path.is_file() or path.stat().st_size <= 0:
            raise SourceDerivedVisualCapsuleError(f"source_visual_parent_artifact_missing:{path.name}")
    timeline = _read_object(timeline_path)
    if timeline.get("status") != "ok":
        raise SourceDerivedVisualCapsuleError("source_visual_parent_timeline_not_ok")
    section = _section_from_timeline(timeline, section_key)
    section_start = _seconds(section.get("start_seconds"), "section_start")
    section_end = _seconds(section.get("end_seconds"), "section_end")
    if section_end <= section_start:
        raise SourceDerivedVisualCapsuleError("source_visual_section_non_positive")
    shots = _section_shots(timeline, section_key)
    if abs(float(shots[0]["start_seconds"]) - section_start) > 0.02 or abs(float(shots[-1]["end_seconds"]) - section_end) > 0.02:
        raise SourceDerivedVisualCapsuleError("source_visual_section_shot_coverage_mismatch")
    if float(duration(picture)) + 0.05 < section_end:
        raise SourceDerivedVisualCapsuleError("source_visual_parent_picture_too_short")
    capsule: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "profile": PROFILE,
        "source_section_id": section_key,
        "source_start_seconds": round(section_start, 3),
        "source_end_seconds": round(section_end, 3),
        "source_duration_seconds": round(section_end - section_start, 3),
        "parent_picture_file": "picture.mp4",
        "parent_picture_sha256": sha256_file(picture),
        "parent_final_file": "final.mp4",
        "parent_final_sha256": sha256_file(final),
        "parent_visual_timeline_file": "visual-timeline.json",
        "parent_visual_timeline_sha256": sha256_file(timeline_path),
        "parent_rights_manifest_file": "rights-manifest.json",
        "parent_rights_manifest_sha256": sha256_file(rights_path),
        "timeline_mode": _clean(timeline.get("timeline_mode")),
        "cut_policy": dict(timeline.get("cut_policy") or {}),
        "shots": shots,
        "inheritance_policy": {
            "actual_parent_video_required": True,
            "textual_visual_query_is_not_video_authority": True,
            "new_stock_search_allowed": False,
            "new_vision_review_allowed": False,
            "parent_shot_order_preserved": True,
        },
    }
    capsule["capsule_sha256"] = _canonical_hash(capsule)
    return capsule


def validate_parent_visual_capsule(parent_output_dir: Path, capsule: dict[str, Any]) -> dict[str, Any]:
    root = Path(parent_output_dir)
    certified = validate_capsule_document(capsule)
    expected = {
        "parent_picture_sha256": root / str(certified.get("parent_picture_file") or "picture.mp4"),
        "parent_final_sha256": root / str(certified.get("parent_final_file") or "final.mp4"),
        "parent_visual_timeline_sha256": root / str(certified.get("parent_visual_timeline_file") or "visual-timeline.json"),
        "parent_rights_manifest_sha256": root / str(certified.get("parent_rights_manifest_file") or "rights-manifest.json"),
    }
    for field, path in expected.items():
        if not path.is_file() or sha256_file(path) != _clean(certified.get(field)):
            raise SourceDerivedVisualCapsuleError(f"source_visual_capsule_parent_bytes_changed:{path.name}")
    timeline = _read_object(expected["parent_visual_timeline_sha256"])
    section_id = _clean(certified.get("source_section_id"))
    section = _section_from_timeline(timeline, section_id)
    if abs(_seconds(section.get("start_seconds"), "section_start") - float(certified["source_start_seconds"])) > 0.02:
        raise SourceDerivedVisualCapsuleError("source_visual_capsule_section_start_changed")
    if abs(_seconds(section.get("end_seconds"), "section_end") - float(certified["source_end_seconds"])) > 0.02:
        raise SourceDerivedVisualCapsuleError("source_visual_capsule_section_end_changed")
    if _section_shots(timeline, section_id) != list(certified.get("shots") or []):
        raise SourceDerivedVisualCapsuleError("source_visual_capsule_shots_changed")
    return certified


def _representative_shots(shots: list[dict[str, Any]], target_seconds: float) -> list[dict[str, Any]]:
    if not shots:
        raise SourceDerivedVisualCapsuleError("source_visual_capsule_no_shots")
    if len(shots) <= MAX_INHERITED_SHOTS:
        selected = list(shots)
    else:
        indexes: set[int] = {0, len(shots) - 1}
        for index, shot in enumerate(shots):
            if _clean(shot.get("continuity_role")).casefold() in {"contrast", "counterpoint", "payoff"}:
                indexes.add(index)
                if len(indexes) >= MAX_INHERITED_SHOTS:
                    break
        for slot in range(MAX_INHERITED_SHOTS):
            candidate = round((len(shots) - 1) * slot / max(1, MAX_INHERITED_SHOTS - 1))
            indexes.add(max(0, min(len(shots) - 1, candidate)))
            if len(indexes) >= MAX_INHERITED_SHOTS:
                break
        selected = [shots[index] for index in sorted(indexes)[:MAX_INHERITED_SHOTS]]
    available = sum(float(item["end_seconds"]) - float(item["start_seconds"]) for item in selected)
    if available + 0.05 < target_seconds:
        selected = list(shots)
        available = sum(float(item["end_seconds"]) - float(item["start_seconds"]) for item in selected)
    if available + 0.05 < target_seconds:
        raise SourceDerivedVisualCapsuleError(f"source_visual_capsule_coverage_too_short:available={available:.3f}:target={target_seconds:.3f}")
    return selected


def _allocate_durations(shots: list[dict[str, Any]], target_seconds: float) -> list[float]:
    capacities = [float(item["end_seconds"]) - float(item["start_seconds"]) for item in shots]
    if not capacities:
        raise SourceDerivedVisualCapsuleError("source_visual_capsule_no_duration_slots")
    minimum = min(MIN_INHERITED_SHOT_SECONDS, target_seconds / len(capacities))
    allocations = [min(capacity, minimum) for capacity in capacities]
    remaining = max(0.0, target_seconds - sum(allocations))
    while remaining > 0.01:
        expandable = [i for i, capacity in enumerate(capacities) if capacity - allocations[i] > 0.01]
        if not expandable:
            break
        share = remaining / len(expandable)
        spent = 0.0
        for index in expandable:
            add = min(share, capacities[index] - allocations[index])
            allocations[index] += add
            spent += add
        if spent <= 0.001:
            break
        remaining -= spent
    if abs(sum(allocations) - target_seconds) > 0.06:
        raise SourceDerivedVisualCapsuleError("source_visual_capsule_duration_allocation_failed")
    return allocations


def _extract_portrait(source: Path, dest: Path, *, start: float, seconds: float) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    vf = "scale=1080:1920:force_original_aspect_ratio=increase:flags=lanczos,crop=1080:1920:(iw-1080)/2:(ih-1920)/2,fps=30,setsar=1,format=yuv420p"
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{max(0.0, start):.3f}", "-i", str(source), "-t", f"{seconds:.3f}", "-an", "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", str(dest)],
        check=True,
        env=secret_free_subprocess_env(),
    )
    if not dest.is_file() or dest.stat().st_size <= 1024:
        raise SourceDerivedVisualCapsuleError("source_visual_capsule_extract_failed")
    return dest


def materialize_inherited_parent_visual(parent_output_dir: Path, capsule: dict[str, Any], output_dir: Path, *, target_seconds: float) -> tuple[Path, dict[str, Any]]:
    parent_root = Path(parent_output_dir)
    root = Path(output_dir)
    certified = validate_parent_visual_capsule(parent_root, capsule)
    target = _seconds(target_seconds, "target")
    if target <= 0:
        raise SourceDerivedVisualCapsuleError("source_visual_capsule_target_non_positive")
    if target > float(certified["source_duration_seconds"]) + 0.75:
        raise SourceDerivedVisualCapsuleError("source_visual_capsule_target_exceeds_parent_section:source_safe_reprovision_required=true")
    shots = _representative_shots(list(certified["shots"]), target)
    allocations = _allocate_durations(shots, target)
    source = parent_root / str(certified["parent_picture_file"])
    work = root / "source-derived-parent-visual-v1"
    pieces: list[Path] = []
    used: list[dict[str, Any]] = []
    for index, (shot, seconds) in enumerate(zip(shots, allocations), 1):
        start = float(shot["start_seconds"])
        pieces.append(_extract_portrait(source, work / f"parent-shot-{index:02d}.mp4", start=start, seconds=seconds))
        used.append({"shot_id": shot.get("shot_id"), "source_start_seconds": round(start, 3), "source_end_seconds": round(start + seconds, 3), "short_seconds": round(seconds, 3), "continuity_role": shot.get("continuity_role"), "selected_asset": dict(shot.get("selected_asset") or {}), "final_cut_audit_reference": dict(shot.get("final_cut_audit_reference") or {}), "rights_reference": shot.get("rights_reference")})
    output = work / "picture-source-derived-parent-v1.mp4"
    concat_video(pieces, output)
    actual = float(duration(output))
    if abs(actual - target) > 0.20:
        raise SourceDerivedVisualCapsuleError(f"source_visual_capsule_output_duration_drift:target={target:.3f}:actual={actual:.3f}")
    report = {
        "schema_version": 1,
        "profile": PROFILE,
        "status": "applied",
        "source_section_id": certified["source_section_id"],
        "capsule_sha256": certified["capsule_sha256"],
        "parent_picture_sha256": certified["parent_picture_sha256"],
        "parent_visual_timeline_sha256": certified["parent_visual_timeline_sha256"],
        "parent_rights_manifest_sha256": certified["parent_rights_manifest_sha256"],
        "target_seconds": round(target, 3),
        "used_parent_shots": used,
        "used_parent_shot_count": len(used),
        "actual_parent_video_inherited": True,
        "parent_shot_order_preserved": True,
        "new_stock_assets": 0,
        "extra_stock_queries": 0,
        "extra_vision_ai_calls": 0,
        "extra_text_ai_calls": 0,
        "visual_query_used_as_authority": False,
    }
    (root / "source-derived-parent-visual.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return output, report


def _asset_key(item: dict[str, Any]) -> tuple[str, str]:
    return (_clean(item.get("provider")).lower(), str(item.get("asset_id") or ""))


def inherit_parent_visual_evidence(parent_output_dir: Path, capsule: dict[str, Any], child_output_dir: Path, inheritance_report: dict[str, Any]) -> dict[str, Any]:
    parent_root = Path(parent_output_dir)
    child_root = Path(child_output_dir)
    certified = validate_parent_visual_capsule(parent_root, capsule)
    parent_rights = _read_object(parent_root / str(certified["parent_rights_manifest_file"]))
    child_rights = _read_object(child_root / "rights-manifest.json")
    parent_visuals = [item for item in list(parent_rights.get("visuals") or []) if isinstance(item, dict)]
    if not parent_visuals:
        raise SourceDerivedVisualCapsuleError("source_visual_parent_rights_empty")
    used_keys: list[tuple[str, str]] = []
    audits: list[dict[str, Any]] = []
    for shot in list(inheritance_report.get("used_parent_shots") or []):
        if not isinstance(shot, dict):
            continue
        selected = shot.get("selected_asset") if isinstance(shot.get("selected_asset"), dict) else {}
        key = _asset_key(selected)
        if not key[0] or not key[1]:
            raise SourceDerivedVisualCapsuleError("source_visual_used_shot_provenance_missing")
        if key not in used_keys:
            used_keys.append(key)
        audits.append({"section": "s1", "status": "pass", "review_origin": "inherited_parent_m7_final_cut", "source_parent_section_id": certified["source_section_id"], "source_parent_shot_id": shot.get("shot_id"), "provider": key[0], "candidate_id": key[1], "final_cut_audit_reference": dict(shot.get("final_cut_audit_reference") or {}), "source_parent_visual_timeline_sha256": certified["parent_visual_timeline_sha256"], "new_vision_review_performed": False})
    inherited_rights: list[dict[str, Any]] = []
    for key in used_keys:
        matches = [item for item in parent_visuals if _asset_key(item) == key]
        if len(matches) != 1:
            raise SourceDerivedVisualCapsuleError(f"source_visual_parent_rights_match_failed:{key[0]}:{key[1]}")
        inherited_rights.append(dict(matches[0]))
    child_rights["visuals"] = inherited_rights
    child_rights["source_derived_parent_visual_v1"] = {"profile": PROFILE, "capsule_sha256": certified["capsule_sha256"], "source_section_id": certified["source_section_id"], "parent_rights_manifest_sha256": certified["parent_rights_manifest_sha256"], "actual_parent_video_inherited": True, "used_parent_asset_count": len(inherited_rights), "new_stock_assets": 0, "extra_vision_ai_calls": 0}
    (child_root / "rights-manifest.json").write_text(json.dumps(child_rights, ensure_ascii=False, indent=2), encoding="utf-8")
    (child_root / "visual-audit.json").write_text(json.dumps(audits, ensure_ascii=False, indent=2), encoding="utf-8")
    quality_path = child_root / "quality-final.json"
    quality = _read_object(quality_path)
    quality.update({"visual_sections_reviewed": max(1, int(quality.get("visual_sections_reviewed") or 0)), "source_derived_parent_video_inherited": True, "source_parent_visual_capsule_sha256": certified["capsule_sha256"], "source_parent_visual_timeline_sha256": certified["parent_visual_timeline_sha256"], "visual_query_used_as_final_visual_authority": False})
    quality_path.write_text(json.dumps(quality, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"inherited_visual_rights": len(inherited_rights), "inherited_visual_audits": len(audits), "capsule_sha256": certified["capsule_sha256"], "actual_parent_video_inherited": True}
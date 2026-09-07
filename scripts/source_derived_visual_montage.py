from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scripts import short_cinematic_director as base
from scripts.short_human_editorial_montage import (
    PROFILE,
    _apply_reframes,
    _visual_treatment_spans,
    plan_editorial_boundaries,
)


SOURCE_VISUAL_PROFILE = "source_derived_parent_visual_montage_v2"
MAX_SOURCE_DERIVED_REFRAMES = 2
SOURCE_DERIVED_SECOND_REFRAME_MIN_SECONDS = 12.0
SOURCE_DERIVED_SECOND_REFRAME_MIN_BEATS = 4


def _clean(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def _source_duration(events: list[dict[str, Any]]) -> float:
    if not events:
        return 0.0
    try:
        start = float(events[0].get("start") or 0.0)
        end = float(events[-1].get("end") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, end - start)


def _source_reframe_budget(events: list[dict[str, Any]]) -> int:
    """Return a bounded local-only reframe budget for a source-derived Short.

    A second local treatment is justified only when the derivative has enough semantic
    structure and screen time for it to read as an editorial beat rather than decorative
    motion. This never authorizes another stock search or Vision/Text provider call.
    """
    if (
        len(events) >= SOURCE_DERIVED_SECOND_REFRAME_MIN_BEATS
        and _source_duration(events) >= SOURCE_DERIVED_SECOND_REFRAME_MIN_SECONDS
    ):
        return MAX_SOURCE_DERIVED_REFRAMES
    return 1


def _source_inherited_decisions(
    events: list[dict[str, Any]],
    template: str,
    asset_count: int,
) -> list[dict[str, Any]]:
    """Keep human semantic cuts only when another certified parent asset exists.

    A desired CUT never triggers a new stock search. When the parent section has too few
    usable visuals for the Short's semantic turns, the strongest unserved turns become
    bounded local reframes. A long four-beat derivative may use at most two local
    reframes; shorter derivatives stay at one. Every other boundary remains a HOLD.
    """
    _segments, desired = plan_editorial_boundaries(events, template)
    available_cuts = max(0, min(base.MAX_SHORT_SHOTS, int(asset_count)) - 1)
    reframe_budget = _source_reframe_budget(events)
    used_cuts = 0
    used_reframes = 0
    result: list[dict[str, Any]] = []
    for item in desired:
        decision = str(item.get("decision") or "HOLD")
        reason = str(item.get("reason") or "semantic_continuity")
        if decision == "CUT" and used_cuts < available_cuts:
            used_cuts += 1
            final_decision = "CUT"
            final_reason = f"parent_asset_{reason}"
        elif decision in {"CUT", "SUBTLE_REFRAME"} and used_reframes < reframe_budget:
            used_reframes += 1
            final_decision = "SUBTLE_REFRAME"
            final_reason = f"parent_asset_local_{reason}"
        else:
            final_decision = "HOLD"
            final_reason = "parent_asset_semantic_continuity"
        result.append(
            {
                "from_beat_id": item.get("from_beat_id"),
                "to_beat_id": item.get("to_beat_id"),
                "decision": final_decision,
                "reason": final_reason,
            }
        )
    return result


def _segments_from_decisions(
    events: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if len(events) < 2 or len(decisions) != len(events) - 1:
        raise base.ShortCinematicError("Parent visual montage boundary cardinality mismatch")
    segments: list[dict[str, Any]] = [dict(events[0])]
    segments[0]["_covered_beat_ids"] = ["b01"]
    for event_index, (event, boundary) in enumerate(zip(events[1:], decisions), 2):
        if boundary.get("decision") == "CUT":
            segment = dict(event)
            segment["_covered_beat_ids"] = [f"b{event_index:02d}"]
            segments.append(segment)
            continue
        current = segments[-1]
        current["end"] = event.get("end")
        current.setdefault("_covered_beat_ids", []).append(f"b{event_index:02d}")
        if boundary.get("decision") == "SUBTLE_REFRAME":
            current.setdefault("_reframe_beat_ids", []).append(f"b{event_index:02d}")
    return segments


def _serializable_inheritance(inheritance: dict[str, Any], used_count: int) -> dict[str, Any]:
    assets: list[dict[str, Any]] = []
    for raw in list(inheritance.get("assets") or [])[:used_count]:
        if not isinstance(raw, dict):
            continue
        assets.append(
            {
                "filename": raw.get("filename"),
                "sha256": raw.get("sha256"),
                "duration_seconds": raw.get("duration_seconds"),
                "provider": raw.get("provider"),
                "asset_id": raw.get("asset_id"),
                "credit": raw.get("credit"),
                "rights": raw.get("rights"),
                "parent_audit": raw.get("parent_audit"),
            }
        )
    return {
        "schema_version": 1,
        "mode": inheritance.get("mode"),
        "source_production_plan_sha256": inheritance.get("source_production_plan_sha256"),
        "source_parent_final_sha256": inheritance.get("source_parent_final_sha256"),
        "source_section_id": inheritance.get("source_section_id"),
        "source_section_index": inheritance.get("source_section_index"),
        "source_visual_query": inheritance.get("source_visual_query"),
        "asset_count_available": inheritance.get("asset_count"),
        "asset_count_used": len(assets),
        "total_visual_seconds": inheritance.get("total_visual_seconds"),
        "assets": assets,
        "extra_stock_queries": 0,
        "extra_vision_ai_calls": 0,
        "extra_text_ai_calls": 0,
    }


def _bind_exact_final_visual_provenance(
    root: Path,
    inheritance: dict[str, Any],
    *,
    used_count: int,
    boundary_decisions: list[dict[str, Any]],
) -> dict[str, Any]:
    serial = _serializable_inheritance(inheritance, used_count)
    used_assets = serial["assets"]
    if not used_assets:
        raise base.ShortCinematicError("Parent visual montage has no used inherited assets")

    credits_path = root / "credits.json"
    old_credits = base._read_json(credits_path, list)
    exact_credits: list[dict[str, Any]] = []
    exact_rights: list[dict[str, Any]] = []
    exact_audits: list[dict[str, Any]] = []
    for item in used_assets:
        credit = dict(item.get("credit") or {})
        credit.update(
            {
                "inherited_from_parent_long": True,
                "source_section_id": serial.get("source_section_id"),
                "source_parent_final_sha256": serial.get("source_parent_final_sha256"),
            }
        )
        exact_credits.append(credit)

        rights_entry = dict(item.get("rights") or {})
        rights_entry.update(
            {
                "inherited_from_parent_long": True,
                "source_section_id": serial.get("source_section_id"),
            }
        )
        exact_rights.append(rights_entry)

        audit = dict(item.get("parent_audit") or {})
        audit.update(
            {
                "status": "pass",
                "is_selected": True,
                "review_origin": "parent_long_inherited_certified",
                "vision_review_performed": False,
                "inherited_from_parent_long": True,
                "source_section_id": serial.get("source_section_id"),
            }
        )
        exact_audits.append(audit)

    credits_path.write_text(json.dumps(exact_credits, ensure_ascii=False, indent=2), encoding="utf-8")

    rights_path = root / "rights-manifest.json"
    rights = base._read_json(rights_path, dict)
    previous_visuals = list(rights.get("visuals") or [])
    rights["visuals"] = exact_rights
    rights["source_derived_visual_inheritance"] = {
        **serial,
        "profile": SOURCE_VISUAL_PROFILE,
        "final_visual_authority": True,
        "parent_audit_reused_without_new_vision": True,
        "boundary_decisions": boundary_decisions,
        "provisional_child_core_visual_count": len(previous_visuals),
        "provisional_child_core_visual_superseded": True,
    }
    rights_path.write_text(json.dumps(rights, ensure_ascii=False, indent=2), encoding="utf-8")

    audit_path = root / "visual-audit.json"
    old_audits = base._read_json(audit_path, list)
    (root / "short-sibling-provisional-core-visual-audit.json").write_text(
        json.dumps(old_audits, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    audit_path.write_text(json.dumps(exact_audits, ensure_ascii=False, indent=2), encoding="utf-8")

    serial.update(
        {
            "profile": SOURCE_VISUAL_PROFILE,
            "final_visual_authority": True,
            "parent_audit_reused_without_new_vision": True,
            "provisional_child_core_credit_count": len(old_credits),
            "provisional_child_core_visual_superseded": True,
            "boundary_decisions": boundary_decisions,
        }
    )
    (root / "source-derived-visual-provenance.json").write_text(
        json.dumps(serial, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return serial


def render_parent_inherited_picture(
    root: Path,
    events: list[dict[str, Any]],
    template: str,
    inheritance: dict[str, Any],
) -> tuple[Path, dict[str, Any]]:
    """Build the sibling's final picture from the exact certified section assets of its parent Long."""
    root = Path(root)
    assets = [item for item in list(inheritance.get("assets") or []) if isinstance(item, dict)]
    if not assets:
        raise base.ShortCinematicError("Parent visual montage requires inherited section assets")
    decisions = _source_inherited_decisions(events, template, len(assets))
    segments = _segments_from_decisions(events, decisions)
    if len(segments) > min(len(assets), base.MAX_SHORT_SHOTS):
        raise base.ShortCinematicError("Parent visual montage requested more cuts than inherited assets")

    work = root / "short-sibling-parent-visual-v1"
    work.mkdir(parents=True, exist_ok=True)
    prepared: list[Path] = []
    for index, segment in enumerate(segments, 1):
        raw = assets[index - 1]
        source = raw.get("path")
        if not isinstance(source, Path) or not source.is_file():
            raise base.ShortCinematicError("Parent visual montage inherited asset path is missing")
        seconds = base._event_duration(segment)
        dest = work / f"parent-segment-{index:02d}.mp4"
        base.prepare_clip(source, dest, seconds, portrait=True, fps=30)
        if not dest.is_file() or dest.stat().st_size <= 1024:
            raise base.ShortCinematicError("Parent visual montage failed to prepare inherited segment")
        prepared.append(dest)

    picture = work / "picture-parent-inherited-base-v1.mp4"
    base.concat_video(prepared, picture)
    editorial_picture = _apply_reframes(picture, events, decisions, work)
    progressive = work / "picture-parent-inherited-text-v1.mp4"
    base.render_progressive_text(
        video=editorial_picture,
        events=events,
        srt_path=root / "short-progressive-parent-inherited.srt",
        output=progressive,
    )

    provenance = _bind_exact_final_visual_provenance(
        root,
        inheritance,
        used_count=len(segments),
        boundary_decisions=decisions,
    )
    spans = _visual_treatment_spans(events, decisions)
    max_treatment_span = max(
        (float(item["end"]) - float(item["start"]) for item in spans),
        default=0.0,
    )
    report = {
        "schema_version": 2,
        "profile": PROFILE,
        "source_visual_profile": SOURCE_VISUAL_PROFILE,
        "scope": "short_sibling",
        "status": "applied",
        "source_safe": True,
        "source_video_inherited": True,
        "source_section_id": inheritance.get("source_section_id"),
        "semantic_beat_count": len(events),
        "visual_segment_count": len(segments),
        "visual_treatment_span_count": len(spans),
        "max_visual_treatment_span_seconds": round(max_treatment_span, 3),
        "source_safe_reframe_budget": _source_reframe_budget(events),
        "independent_short_beat_mapping": True,
        "parent_asset_count_available": len(assets),
        "parent_asset_count_used": len(segments),
        "boundary_decisions": decisions,
        "hold_count": sum(item["decision"] == "HOLD" for item in decisions),
        "subtle_reframe_count": sum(item["decision"] == "SUBTLE_REFRAME" for item in decisions),
        "cut_count": sum(item["decision"] == "CUT" for item in decisions),
        "new_stock_assets": 0,
        "extra_stock_queries": 0,
        "extra_vision_ai_calls": 0,
        "extra_text_ai_calls": 0,
        "approved_text_preserved": True,
        "final_visual_provenance": provenance,
    }
    (root / "short-sibling-human-editorial-montage.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return progressive, report

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from scripts import short_cinematic_director as base


PROFILE = "short_human_editorial_montage_v1"

_STRONG_TURN_MARKERS = (
    "لكن",
    " بل ",
    "بينما",
    "بدلًا",
    "بدلا",
    "المشكلة ليست",
    "الحقيقة",
    "الأدق",
    "في الواقع",
    "فجأة",
    "عندها",
    "وهنا",
)


def _clean(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def _decision_for_boundary(
    template: str,
    boundary_index: int,
    total_events: int,
    current_event: dict[str, Any],
) -> tuple[str, str]:
    """Return a deterministic editorial decision before the current semantic beat.

    boundary_index is one-based between events: 1 means event 1 -> event 2.  We never
    invent semantic content and never call a provider here.  The final payoff remains a
    real cut so every standalone Short retains at least two independently audited assets.
    """
    if total_events < 2:
        raise base.ShortCinematicError("Human editorial montage requires at least two semantic beats")
    if boundary_index <= 0 or boundary_index >= total_events:
        raise base.ShortCinematicError("Human editorial montage received an invalid beat boundary")

    if boundary_index == total_events - 1:
        return "CUT", "payoff_boundary"

    text = f" {_clean(current_event.get('text'))} "
    if any(marker in text for marker in _STRONG_TURN_MARKERS):
        return "CUT", "explicit_semantic_turn"

    # Micro stories benefit from one early scene/action change.  Other templates are
    # deliberately more restrained and keep the opening image when meaning continues.
    if template == "micro_story" and boundary_index == 1:
        return "CUT", "micro_story_scene_progression"

    # A reframe is emphasis without pretending that a new semantic visual is required.
    # It remains on the same already-audited asset and is executed locally with FFmpeg.
    if template in {"inner_dialogue", "quote_reflection", "micro_story"} and boundary_index == 2:
        return "SUBTLE_REFRAME", "continuity_with_emphasis"

    return "HOLD", "semantic_continuity"


def plan_editorial_boundaries(
    events: list[dict[str, Any]],
    template: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Collapse HOLD/REFRAME beats into visual segments while preserving all text beats."""
    if template not in base._TEMPLATE_QUERY_MODIFIERS:
        raise base.ShortCinematicError("Human editorial montage received unsupported Short template")
    if len(events) < 2:
        raise base.ShortCinematicError("Human editorial montage requires at least two semantic beats")

    segments: list[dict[str, Any]] = [dict(events[0])]
    segments[0]["_covered_beat_ids"] = ["b01"]
    decisions: list[dict[str, Any]] = []

    for event_index, event in enumerate(events[1:], 2):
        boundary_index = event_index - 1
        decision, reason = _decision_for_boundary(
            template,
            boundary_index,
            len(events),
            event,
        )
        decisions.append(
            {
                "from_beat_id": f"b{event_index - 1:02d}",
                "to_beat_id": f"b{event_index:02d}",
                "decision": decision,
                "reason": reason,
            }
        )
        if decision == "CUT":
            segment = dict(event)
            segment["_covered_beat_ids"] = [f"b{event_index:02d}"]
            segments.append(segment)
            continue

        current = segments[-1]
        current["end"] = event.get("end")
        joined = " ".join(
            value for value in (_clean(current.get("text")), _clean(event.get("text"))) if value
        )
        current["text"] = joined
        current.setdefault("_covered_beat_ids", []).append(f"b{event_index:02d}")
        if decision == "SUBTLE_REFRAME":
            current.setdefault("_reframe_beat_ids", []).append(f"b{event_index:02d}")

    if len(segments) < base.MIN_SHORT_SHOTS:
        # The payoff boundary above should make this unreachable for a valid Short.
        raise base.ShortCinematicError("Human editorial montage restraint removed required visual coverage")
    if len(segments) > base.MAX_SHORT_SHOTS:
        raise base.ShortCinematicError("Human editorial montage exceeded Short shot ceiling")
    return segments, decisions


def _video_dimensions(path: Path) -> tuple[int, int]:
    completed = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    raw = completed.stdout.strip().split("x", 1)
    if len(raw) != 2:
        raise base.ShortCinematicError("Human editorial montage cannot resolve picture dimensions")
    width, height = int(raw[0]), int(raw[1])
    if width <= 0 or height <= 0:
        raise base.ShortCinematicError("Human editorial montage received invalid picture dimensions")
    return width, height


def _subtle_reframe(source: Path, output: Path) -> Path:
    width, height = _video_dimensions(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    vf = (
        f"crop=iw/1.035:ih/1.035:(iw-ow)/2:(ih-oh)/2,"
        f"scale={width}:{height},fps=30,setsar=1,format=yuv420p"
    )
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(source), "-an", "-vf", vf,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", str(output),
        ],
        check=True,
    )
    if not output.is_file() or output.stat().st_size <= 1024:
        raise base.ShortCinematicError("Human editorial montage reframe did not produce a usable picture")
    return output


def _apply_reframes(
    picture: Path,
    events: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    work: Path,
) -> Path:
    reframe_beats = {
        item["to_beat_id"]
        for item in decisions
        if item.get("decision") == "SUBTLE_REFRAME"
    }
    if not reframe_beats:
        return picture

    pieces: list[Path] = []
    piece_root = work / "editorial-pieces"
    piece_root.mkdir(parents=True, exist_ok=True)
    for index, event in enumerate(events, 1):
        start = float(event.get("start") or 0.0)
        seconds = base._event_duration(event)
        raw_piece = base._trim_video(
            picture,
            piece_root / f"beat-{index:02d}-raw.mp4",
            start,
            seconds,
        )
        if f"b{index:02d}" in reframe_beats:
            piece = _subtle_reframe(raw_piece, piece_root / f"beat-{index:02d}-reframe.mp4")
        else:
            piece = raw_piece
        pieces.append(piece)

    output = work / "picture-short-human-editorial-v1.mp4"
    base.concat_video(pieces, output)
    expected = sum(base._event_duration(item) for item in events)
    actual = base.duration(output)
    if abs(actual - expected) > 0.20:
        raise base.ShortCinematicError(
            f"Human editorial montage duration drift: expected={expected:.3f} actual={actual:.3f}"
        )
    return output


def _patch_receipts(
    root: Path,
    updated: dict[str, Any],
    original_events: list[dict[str, Any]],
    visual_segments: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
) -> dict[str, Any]:
    timeline_path = root / "short-visual-timeline.json"
    timeline = base._read_json(timeline_path, dict)
    shots = [item for item in list(timeline.get("shots") or []) if isinstance(item, dict)]
    if len(shots) != len(visual_segments):
        raise base.ShortCinematicError("Human editorial montage visual-segment receipt mismatch")
    for shot, segment in zip(shots, visual_segments):
        shot["covered_beat_ids"] = list(segment.get("_covered_beat_ids") or [])

    hold_count = sum(item.get("decision") == "HOLD" for item in decisions)
    reframe_count = sum(item.get("decision") == "SUBTLE_REFRAME" for item in decisions)
    cut_count = sum(item.get("decision") == "CUT" for item in decisions)
    avoided = hold_count + reframe_count
    timeline.update(
        {
            "human_editorial_montage_profile": PROFILE,
            "semantic_beat_count": len(original_events),
            "beat_to_shot_binding": "semantic_boundary_hold_cut_reframe",
            "transition_policy": "semantic_cut_with_restraint",
            "boundary_decisions": decisions,
            "hold_count": hold_count,
            "subtle_reframe_count": reframe_count,
            "cut_count": cut_count,
            "additional_stock_assets_avoided": avoided,
            "additional_vision_boundaries_avoided": avoided,
            "extra_ai_calls_added_by_human_montage": 0,
            "shots": shots,
        }
    )
    timeline_path.write_text(json.dumps(timeline, ensure_ascii=False, indent=2), encoding="utf-8")

    rights_path = root / "rights-manifest.json"
    rights = base._read_json(rights_path, dict)
    cinematic_rights = rights.get("short_cinematic_v1")
    if not isinstance(cinematic_rights, dict):
        raise base.ShortCinematicError("Human editorial montage requires Short cinematic rights receipt")
    cinematic_rights.update(
        {
            "human_editorial_montage_profile": PROFILE,
            "semantic_beat_count": len(original_events),
            "boundary_decision_policy": "semantic_cut_with_restraint",
            "additional_stock_assets_avoided": avoided,
            "extra_ai_calls_added_by_human_montage": 0,
        }
    )
    rights_path.write_text(json.dumps(rights, ensure_ascii=False, indent=2), encoding="utf-8")

    result = dict(updated)
    result["timed_text_events"] = original_events
    compensation = dict(result.get("compensation") or {})
    compensation.update(
        {
            "human_editorial_montage_profile": PROFILE,
            "human_editorial_hold_count": hold_count,
            "human_editorial_cut_count": cut_count,
            "human_editorial_subtle_reframe_count": reframe_count,
            "beat_driven_visual_reframe_applied": reframe_count > 0,
            "additional_stock_assets_avoided": avoided,
            "additional_vision_boundaries_avoided": avoided,
            "extra_ai_calls_added_by_human_montage": 0,
        }
    )
    result["compensation"] = compensation
    result["short_cinematic"] = timeline
    return result


def upgrade_short_cinematic(
    output_dir: Path,
    control_request: dict[str, Any],
    pre_gold: dict[str, Any],
    *,
    ledger: Any,
) -> dict[str, Any]:
    """Human editorial boundary layer over the certified Short cinematic director.

    CUT still delegates to the existing audited stock/Visual-QA/M8/rights pipeline.
    HOLD and SUBTLE_REFRAME consume no new provider call.  All original text events are
    restored after visual composition so editorial restraint never removes approved copy.
    """
    if control_request.get("kind") != "short" or _clean(control_request.get("approval_scope")) != "short_only":
        return base.upgrade_short_cinematic(output_dir, control_request, pre_gold, ledger=ledger)

    root = Path(output_dir)
    template = _clean(pre_gold.get("short_template"))
    original_events = [
        dict(item) for item in list(pre_gold.get("timed_text_events") or []) if isinstance(item, dict)
    ]
    visual_segments, decisions = plan_editorial_boundaries(original_events, template)

    delegated = dict(pre_gold)
    delegated["timed_text_events"] = visual_segments
    updated = base.upgrade_short_cinematic(root, control_request, delegated, ledger=ledger)

    work = root / "short-cinematic-v1"
    cinematic_picture = work / "picture-short-cinematic-v1.mp4"
    if not cinematic_picture.is_file():
        raise base.ShortCinematicError("Human editorial montage requires cinematic picture before text")
    editorial_picture = _apply_reframes(cinematic_picture, original_events, decisions, work)

    progressive_picture = work / "picture-short-human-editorial-text-v1.mp4"
    base.render_progressive_text(
        video=editorial_picture,
        events=original_events,
        srt_path=root / "short-progressive-cinematic.srt",
        output=progressive_picture,
    )
    final_path = root / "final.mp4"
    remuxed = work / "final-short-human-editorial-v1.mp4"
    base._remux_video_with_existing_audio(progressive_picture, final_path, remuxed)
    shutil.move(str(remuxed), str(final_path))

    return _patch_receipts(root, updated, original_events, visual_segments, decisions)

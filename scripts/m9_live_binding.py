from __future__ import annotations

import json
import math
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import isco_video_agent.orchestrator as orchestrator
from isco_video_agent.media.ffmpeg import duration
from isco_video_agent.security import secret_free_subprocess_env


_REPORT_NAME = "m9-transitions.json"
_TIMELINE_NAME = "visual-timeline.json"
_DISSOLVE_SECONDS = 0.36
_OPENING_GUARD_SECONDS = 30.0
_ENDING_GUARD_SECONDS = 12.0
_MAX_DISSOLVE_RATIO = 0.15

_POSITIVE_INTENT = (
    "continue", "continuity", "flow", "carry", "bridge", "linger", "hold", "settle", "resolve", "land",
    "استمرار", "استمرارية", "امتداد", "تدفق", "جسر", "هدوء", "استقرار", "حل",
)
_HARD_CUT_INTENT = (
    "cut", "contrast", "turn", "shift", "snap", "break", "jolt", "reveal", "interrupt",
    "قطع", "تباين", "تحول", "انعطاف", "صدمة", "كسر", "كشف",
)
_CONTINUITY_ROLES = frozenset({"develop", "callback", "payoff"})


def _text(value: object) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _motifs(shot: dict[str, Any]) -> set[str]:
    raw = shot.get("motif_ids")
    if not isinstance(raw, list):
        return set()
    return {str(item).strip() for item in raw if str(item).strip()}


def _continuity_evidence(left: dict[str, Any], right: dict[str, Any]) -> tuple[bool, str]:
    intent = _text(right.get("transition_intent") or left.get("transition_intent"))
    if any(marker in intent for marker in _HARD_CUT_INTENT):
        return False, "transition_intent_requests_semantic_break"

    left_role = _text(left.get("continuity_role"))
    right_role = _text(right.get("continuity_role"))
    if left_role == "contrast" or right_role in {"contrast", "establish"}:
        return False, "continuity_role_requires_cut"

    intent_continuity = any(marker in intent for marker in _POSITIVE_INTENT)
    role_continuity = right_role in _CONTINUITY_ROLES and left_role in (_CONTINUITY_ROLES | {"establish"})
    shared_motif = bool(_motifs(left) & _motifs(right))

    if intent_continuity and (role_continuity or shared_motif):
        return True, "explicit_intent_plus_continuity_evidence"
    if role_continuity and shared_motif:
        return True, "continuity_role_plus_shared_motif"
    return False, "insufficient_continuity_evidence"


def plan_semantic_transitions(timeline: dict[str, Any]) -> dict[str, Any]:
    shots = timeline.get("final_cut_visuals")
    if not isinstance(shots, list) or len(shots) < 2:
        return {
            "version": "m9-current-m7-v1",
            "status": "hard_cut_only",
            "dissolve_seconds": _DISSOLVE_SECONDS,
            "boundaries": [],
            "dissolve_count": 0,
            "hard_cut_count": max(0, len(shots or []) - 1),
            "zero_additional_ai_calls": True,
        }

    boundaries: list[dict[str, Any]] = []
    total_duration = float(shots[-1].get("end_seconds") or 0.0)
    boundary_count = len(shots) - 1
    max_dissolves = math.floor(boundary_count * _MAX_DISSOLVE_RATIO + 1e-9)
    # A long episode with 7+ boundaries may earn at least one dissolve when evidence is strong.
    if boundary_count >= 7:
        max_dissolves = max(1, max_dissolves)

    chosen = 0
    previous_was_dissolve = False
    for index in range(boundary_count):
        left, right = shots[index], shots[index + 1]
        boundary_time = float(left.get("end_seconds") or right.get("start_seconds") or 0.0)
        kind = "hard_cut"
        reason = "hard_cut_default"

        if previous_was_dissolve:
            reason = "adjacent_dissolve_forbidden"
        elif boundary_time < _OPENING_GUARD_SECONDS:
            reason = "opening_guard"
        elif total_duration > 0 and boundary_time > total_duration - _ENDING_GUARD_SECONDS:
            reason = "ending_guard"
        elif chosen >= max_dissolves:
            reason = "dissolve_density_cap"
        else:
            eligible, reason = _continuity_evidence(left, right)
            if eligible:
                kind = "dissolve"
                chosen += 1

        boundaries.append(
            {
                "boundary_index": index,
                "left_shot_id": left.get("shot_id"),
                "right_shot_id": right.get("shot_id"),
                "boundary_seconds": round(boundary_time, 3),
                "transition": kind,
                "reason": reason,
            }
        )
        previous_was_dissolve = kind == "dissolve"

    return {
        "version": "m9-current-m7-v1",
        "status": "applied" if chosen else "hard_cut_only",
        "m7_timeline_preserved": True,
        "m7_transition_type_mutated": False,
        "dissolve_seconds": _DISSOLVE_SECONDS,
        "max_dissolve_ratio": _MAX_DISSOLVE_RATIO,
        "opening_guard_seconds": _OPENING_GUARD_SECONDS,
        "ending_guard_seconds": _ENDING_GUARD_SECONDS,
        "dissolve_count": chosen,
        "hard_cut_count": boundary_count - chosen,
        "boundaries": boundaries,
        "zero_additional_ai_calls": True,
    }


def _render_pair(left: Path, right: Path, dest: Path, *, dissolve_seconds: float = _DISSOLVE_SECONDS) -> Path:
    left_seconds = duration(left)
    right_seconds = duration(right)
    if left_seconds <= dissolve_seconds or right_seconds <= dissolve_seconds:
        raise RuntimeError("m9_pair_too_short_for_timing_preserving_dissolve")
    half = dissolve_seconds / 2.0
    offset = left_seconds - half
    dest.parent.mkdir(parents=True, exist_ok=True)
    vf = (
        f"[0:v]tpad=stop_mode=clone:stop_duration={half:.6f},settb=AVTB,setpts=PTS-STARTPTS[v0];"
        f"[1:v]tpad=start_mode=clone:start_duration={half:.6f},settb=AVTB,setpts=PTS-STARTPTS[v1];"
        f"[v0][v1]xfade=transition=fade:duration={dissolve_seconds:.6f}:offset={offset:.6f},"
        "fps=30,setsar=1,format=yuv420p[v]"
    )
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(left), "-i", str(right),
            "-filter_complex", vf, "-map", "[v]", "-an",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", str(dest),
        ],
        check=True,
        env=secret_free_subprocess_env(),
    )
    expected = left_seconds + right_seconds
    actual = duration(dest)
    if abs(actual - expected) > 0.12:
        dest.unlink(missing_ok=True)
        raise RuntimeError(
            f"m9_timing_invariant_failed expected={expected:.3f} actual={actual:.3f}"
        )
    return dest


def _write_report(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


@contextmanager
def m9_live_scope() -> Iterator[None]:
    original_concat = orchestrator.concat_video

    def concat_bound(inputs, output):
        output = Path(output)
        paths = [Path(item) for item in inputs]
        if output.name != "picture.mp4":
            return original_concat(paths, output)

        report_path = output.parent / _REPORT_NAME
        timeline_path = output.parent / _TIMELINE_NAME
        try:
            timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
        except Exception as exc:
            _write_report(report_path, {
                "version": "m9-current-m7-v1", "status": "skipped",
                "reason": f"timeline_unavailable:{type(exc).__name__}",
                "zero_additional_ai_calls": True,
            })
            return original_concat(paths, output)

        shots = timeline.get("final_cut_visuals") if isinstance(timeline, dict) else None
        if not isinstance(shots, list) or len(shots) != len(paths):
            _write_report(report_path, {
                "version": "m9-current-m7-v1", "status": "skipped",
                "reason": "m7_final_cut_input_count_mismatch",
                "timeline_shots": len(shots) if isinstance(shots, list) else None,
                "concat_inputs": len(paths),
                "zero_additional_ai_calls": True,
            })
            return original_concat(paths, output)

        plan = plan_semantic_transitions(timeline)
        _write_report(report_path, plan)
        dissolve_boundaries = {
            int(item["boundary_index"])
            for item in plan.get("boundaries", [])
            if item.get("transition") == "dissolve"
        }
        if not dissolve_boundaries:
            return original_concat(paths, output)

        temp_dir = output.parent / ".m9"
        temp_dir.mkdir(parents=True, exist_ok=True)
        rendered: list[Path] = []
        index = 0
        try:
            while index < len(paths):
                if index in dissolve_boundaries:
                    pair = temp_dir / f"pair-{index:03d}.mp4"
                    rendered.append(_render_pair(paths[index], paths[index + 1], pair))
                    index += 2
                else:
                    rendered.append(paths[index])
                    index += 1
            result = original_concat(rendered, output)
            source_total = sum(duration(path) for path in paths)
            final_total = duration(Path(result))
            if abs(final_total - source_total) > 0.18:
                raise RuntimeError(
                    f"m9_final_timing_invariant_failed expected={source_total:.3f} actual={final_total:.3f}"
                )
            return result
        finally:
            for item in temp_dir.glob("*.mp4"):
                item.unlink(missing_ok=True)
            try:
                temp_dir.rmdir()
            except OSError:
                pass

    orchestrator.concat_video = concat_bound
    try:
        yield
    finally:
        orchestrator.concat_video = original_concat


def install_m9_live_binding() -> None:
    current = orchestrator.produce
    if getattr(current, "_isco_m9_live_binding", False):
        return

    def wrapped(*args, **kwargs):
        with m9_live_scope():
            return current(*args, **kwargs)

    wrapped._isco_m9_live_binding = True
    wrapped._isco_m9_original = current
    orchestrator.produce = wrapped

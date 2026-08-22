from __future__ import annotations

import json
from pathlib import Path

import isco_video_agent.orchestrator as orchestrator
from isco_video_agent.cinematic_v2_runtime import (
    apply_planned_cards,
    build_cinematic_timeline,
    prepare_clip_with_m8,
)


_MARKER = "_isco_cinematic_v2_runtime_installed"
_original_duration = orchestrator.duration
_original_prepare_clip = orchestrator.prepare_clip
_original_concat_video = orchestrator.concat_video

_section_durations: list[float] = []
_color_reports: list[dict] = []


def _reset_run_state() -> None:
    _section_durations.clear()
    _color_reports.clear()


def _duration_with_timeline_capture(path: Path) -> float:
    value = _original_duration(path)
    p = Path(path)
    if p.suffix.lower() == ".wav" and p.parent.name == "audio" and p.stem.isdigit():
        index = int(p.stem)
        while len(_section_durations) < index:
            _section_durations.append(0.0)
        _section_durations[index - 1] = float(value)
    return value


def _prepare_clip_with_cinematic_v2(src, dest, seconds, portrait, fps=30):
    rendered, report = prepare_clip_with_m8(
        Path(src),
        Path(dest),
        float(seconds),
        portrait=bool(portrait),
        fps=int(fps),
        legacy_prepare_clip=_original_prepare_clip,
    )
    if report is not None:
        _color_reports.append({"source": str(src), "dest": str(dest), **report})
    # `moment` has no narrated WAV timeline; capture its one real visual duration.
    if bool(portrait) and not _section_durations:
        _section_durations.append(float(seconds))
    return rendered


def _load_plan(output: Path) -> dict:
    plan_path = output.parent / "plan.json"
    if not plan_path.is_file():
        raise RuntimeError("Cinematic V2 runtime blocked: plan.json missing before timeline binding")
    data = json.loads(plan_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("Cinematic V2 runtime blocked: plan.json must be a JSON object")
    return data


def _concat_video_with_cinematic_v2(inputs, output):
    output = Path(output)
    plan = _load_plan(output)
    sections = plan.get("sections") or []
    if len(_section_durations) != len(sections):
        raise RuntimeError(
            "Cinematic V2 runtime blocked: captured section timeline does not match plan "
            f"({len(_section_durations)} durations vs {len(sections)} sections)"
        )
    timeline = build_cinematic_timeline(plan, list(_section_durations))
    transitions = timeline.get("transition_anchors") or []
    unsupported = [x for x in transitions if x.get("kind") != "HARD_CUT"]
    if unsupported:
        raise RuntimeError(
            "M9 semantic transition renderer blocked: a non-hard-cut transition was authorized "
            "but the production renderer is intentionally fail-closed until that exact dissolve path is bound"
        )
    archive = timeline.get("archive_opportunities") or []
    if archive:
        raise RuntimeError(
            "M11 semantic archive placement blocked: explicit archive evidence exists but automated "
            "CC0 acquisition/render binding has not been authorized for this production path"
        )

    (output.parent / "cinematic-timeline.json").write_text(
        json.dumps(timeline, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output.parent / "color-normalization.json").write_text(
        json.dumps(_color_reports, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    base = _original_concat_video(inputs, output)
    portrait = str(plan.get("format") or "").strip().lower() == "moment"
    return apply_planned_cards(base, timeline, output.parent, portrait=portrait)


def install_cinematic_v2_runtime() -> None:
    """Bind verified Cinematic V2 kernels to the live Runner production seams.

    Ownership remains explicit:
    - Engine owns M7/M8/M9/M10/M11 semantics and render kernels.
    - Runner owns the production-time installation seam, like the existing planner/voice patches.
    - TTS mastering and SFX remain blocked in the timeline until their documented human inputs exist.
    """
    if getattr(orchestrator, _MARKER, False):
        return
    _reset_run_state()
    orchestrator.duration = _duration_with_timeline_capture
    orchestrator.prepare_clip = _prepare_clip_with_cinematic_v2
    orchestrator.concat_video = _concat_video_with_cinematic_v2
    setattr(orchestrator, _MARKER, True)
    print(
        "Cinematic V2 runtime installed: M7 timeline + M8 pre-grade normalization + "
        "M9 hard-cut semantic contract + M10 evidence-bound cards; "
        "TTS mastering=WAIT_TTS_RESULT SFX=WAIT_HUMAN_CURATION"
    )

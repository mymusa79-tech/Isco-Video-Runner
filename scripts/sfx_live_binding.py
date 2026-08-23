from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import isco_video_agent.orchestrator as orchestrator
from isco_video_agent.cinematic_sfx import (
    materialize_sfx_library,
    mix_sfx_into_narration,
    plan_sfx_accents,
    sfx_plan_document,
)


_TIMELINE_NAME = "visual-timeline.json"
_PLAN_NAME = "sfx-plan.json"


@contextmanager
def sfx_live_scope() -> Iterator[None]:
    """Mix restrained procedural SFX after M7 timeline creation and before final mux."""
    original_mux = orchestrator.mux

    def mux_bound(video, narration, output, music=None, **kwargs):
        out_path = Path(output)
        if out_path.name != "final.mp4" or narration is None:
            return original_mux(video, narration, output, music, **kwargs)

        timeline_path = out_path.parent / _TIMELINE_NAME
        if not timeline_path.is_file():
            raise RuntimeError("procedural_sfx_requires_live_m7_visual_timeline")
        try:
            timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("procedural_sfx_invalid_m7_visual_timeline") from exc
        if not isinstance(timeline, dict) or not isinstance(timeline.get("sections"), list):
            raise RuntimeError("procedural_sfx_invalid_m7_sections_contract")

        events = plan_sfx_accents(timeline)
        plan = sfx_plan_document(timeline)
        plan.update(
            {
                "status": "mixed" if events else "no_events",
                "production_stage": "post_mastering_pre_final_mux",
                "source_narration": Path(narration).name,
            }
        )
        (out_path.parent / _PLAN_NAME).write_text(
            json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if not events:
            return original_mux(video, narration, output, music, **kwargs)

        library_root = out_path.parent / "sfx" / "library"
        materialize_sfx_library(library_root)
        mixed = out_path.parent / "narration-sfx.wav"
        mixed = mix_sfx_into_narration(Path(narration), events, library_root, mixed)
        return original_mux(video, mixed, output, music, **kwargs)

    orchestrator.mux = mux_bound
    try:
        yield
    finally:
        orchestrator.mux = original_mux


def install_sfx_live_binding() -> None:
    current = orchestrator.produce
    if getattr(current, "_isco_sfx_live_binding", False):
        return

    def wrapped(*args, **kwargs):
        with sfx_live_scope():
            return current(*args, **kwargs)

    wrapped._isco_sfx_live_binding = True
    wrapped._isco_sfx_original = current
    orchestrator.produce = wrapped

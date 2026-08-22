from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

import isco_video_agent.cinematic_m7_live_binding as engine_m7
import isco_video_agent.orchestrator as orchestrator
from isco_video_agent.anti_repetition import recent_videos
from isco_video_agent.human_editorial_intent import apply_human_editorial_intent


@contextmanager
def _human_editorial_intent_scope():
    """Bind deterministic editorial metadata at the final M7 timeline write seam.

    This wrapper adds no AI calls and does not own cuts, asset selection, transitions,
    rendering, rights, Gold, or history acceptance. It only enriches the persisted
    visual timeline and records its structural signature in accepted history.
    """
    original_write_timeline = engine_m7._write_timeline
    original_append_history = orchestrator.append_history
    state: dict[str, str | None] = {"signature": None}

    def write_timeline_bound(output_dir: Path, timeline: dict) -> None:
        scene_plan_path = Path(output_dir) / "scene_plan.json"
        scene_plan = engine_m7._read_json(scene_plan_path)
        recent_signatures = [
            str(item.get("editorial_visual_signature") or "").strip()
            for item in recent_videos(6)
            if isinstance(item, dict) and item.get("editorial_visual_signature")
        ]
        enriched = apply_human_editorial_intent(
            timeline,
            scene_plan=scene_plan,
            recent_signatures=recent_signatures,
        )
        diversity = enriched.get("human_editorial_intent", {}).get("episode_diversity", {})
        signature = diversity.get("visual_structure_signature")
        state["signature"] = str(signature).strip() if signature else None
        original_write_timeline(Path(output_dir), enriched)

    def append_history_bound(record: dict):
        replacement = dict(record)
        if state["signature"]:
            replacement["editorial_visual_signature"] = state["signature"]
        return original_append_history(replacement)

    engine_m7._write_timeline = write_timeline_bound
    orchestrator.append_history = append_history_bound
    try:
        yield
    finally:
        engine_m7._write_timeline = original_write_timeline
        orchestrator.append_history = original_append_history


def install_m7_live_binding() -> None:
    """Wrap exactly one production call with the Engine's M7 final-render seams.

    The wrapper captures provider keys from the in-process environment before the Engine
    consumes/removes them. It does not add AI calls or change provider routing.
    """
    current = orchestrator.produce
    if getattr(current, "_isco_m7_live_binding", False):
        return

    def wrapped(*args, **kwargs):
        pexels = (os.environ.get("PEXELS_API_KEY") or "").strip()
        pixabay = (os.environ.get("PIXABAY_API_KEY") or "").strip()
        if not pexels:
            # Preserve the core's own authoritative missing-secret failure.
            return current(*args, **kwargs)
        with _human_editorial_intent_scope():
            with engine_m7.live_m7_binding_scope(
                orchestrator,
                pexels_api_key=pexels,
                pixabay_api_key=pixabay,
            ):
                return current(*args, **kwargs)

    wrapped._isco_m7_live_binding = True
    wrapped._isco_m7_original = current
    orchestrator.produce = wrapped

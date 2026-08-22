from __future__ import annotations

import os
from contextlib import contextmanager
from importlib import import_module
from pathlib import Path
from typing import Callable

import isco_video_agent.cinematic_m7_live_binding as engine_m7
import isco_video_agent.orchestrator as orchestrator
from isco_video_agent.anti_repetition import recent_videos
from isco_video_agent.cinematic_m7_live_binding import live_m7_binding_scope
from scripts.security_v1_live_binding import install_security_v1_live_binding


_HUMAN_EDITORIAL_INTENT_MODULE = "isco_video_agent.human_editorial_intent"


def _load_human_editorial_intent() -> Callable | None:
    """Load HEI only when the matching Engine candidate is present.

    Runner can be reviewed while Production V4 is still pinned to the pre-HEI
    Engine. Only absence of this exact new module is a supported no-op. Missing
    transitive dependencies and unrelated import failures remain authoritative.
    """
    try:
        module = import_module(_HUMAN_EDITORIAL_INTENT_MODULE)
    except ModuleNotFoundError as exc:
        if exc.name == _HUMAN_EDITORIAL_INTENT_MODULE:
            return None
        raise
    return module.apply_human_editorial_intent


@contextmanager
def _human_editorial_intent_scope():
    """Bind deterministic editorial metadata at M7's final timeline write seam.

    The layer adds no AI calls and owns no cuts, timing, assets, transitions,
    rendering, rights, Gold decisions, or history acceptance. Before HEI is
    atomically activated in Engine this scope is a strict no-op, preserving the
    currently certified Security V1 + Editorial Room + M7 production behavior.
    """
    apply_human_editorial_intent = _load_human_editorial_intent()
    if apply_human_editorial_intent is None:
        yield
        return

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
    """Install M7 + optional HEI seams, then the outer Security V1 boundary.

    Provider keys are captured before Engine consumes/removes them. HEI, when
    present, enriches only the persisted M7 timeline. Security V1 is installed
    after the M7 wrapper so its production preflight remains outermost at
    invocation time. No layer here adds AI calls or changes provider routing.
    """
    current = orchestrator.produce
    if getattr(current, "_isco_m7_live_binding", False):
        install_security_v1_live_binding()
        return

    def wrapped(*args, **kwargs):
        pexels = (os.environ.get("PEXELS_API_KEY") or "").strip()
        pixabay = (os.environ.get("PIXABAY_API_KEY") or "").strip()
        if not pexels:
            # Preserve the core's own authoritative missing-secret failure.
            return current(*args, **kwargs)
        with _human_editorial_intent_scope():
            # Keep this public symbol so the established M7 contract test can
            # patch the exact production seam while HEI uses engine_m7 internals.
            with live_m7_binding_scope(
                orchestrator,
                pexels_api_key=pexels,
                pixabay_api_key=pixabay,
            ):
                return current(*args, **kwargs)

    wrapped._isco_m7_live_binding = True
    wrapped._isco_m7_original = current
    orchestrator.produce = wrapped
    install_security_v1_live_binding()

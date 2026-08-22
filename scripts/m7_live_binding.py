from __future__ import annotations

import os
from contextlib import contextmanager
from importlib import import_module
from pathlib import Path
from typing import Any, Callable

import isco_video_agent.cinematic_m7_live_binding as engine_m7
import isco_video_agent.orchestrator as orchestrator
from isco_video_agent.anti_repetition import recent_videos
from isco_video_agent.cinematic_m7_live_binding import live_m7_binding_scope
from scripts.security_v1_live_binding import install_security_v1_live_binding


_HUMAN_EDITORIAL_INTENT_MODULE = "isco_video_agent.human_editorial_intent"
_M11_RUNTIME_MODULE = "isco_video_agent.cinematic_m11_runtime"


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


def _load_m11_runtime() -> Any | None:
    """Load M11 only with an Engine that actually carries the certified runtime.

    The Runner remains backward-compatible while Production V4 is pinned to the
    previous Engine. Any import failure other than absence of this exact module is
    authoritative and must not be hidden.
    """
    try:
        return import_module(_M11_RUNTIME_MODULE)
    except ModuleNotFoundError as exc:
        if exc.name == _M11_RUNTIME_MODULE:
            return None
        raise


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


@contextmanager
def _m11_archive_scope(*, smithsonian_api_key: str = ""):
    """Bind M11 after M7 has chosen safe stock, before final concat/rights.

    M11 can replace only an already-qualified body shot and can always degrade back
    to that stock shot. The Engine M11 module owns opportunity limits, CC0 evidence,
    acquisition integrity, rendering, and the durable m11-report.json artifact.
    """
    runtime = _load_m11_runtime()
    if runtime is None:
        yield
        return

    original_materialize = engine_m7.materialize_semantic_body

    def materialize_bound(timeline: dict, **kwargs):
        prepared, credits, audits = original_materialize(timeline, **kwargs)
        output_dir = Path(kwargs["out"])
        scene_plan = engine_m7._read_json(output_dir / "scene_plan.json")
        prepared, credits, report = runtime.apply_m11_overrides(
            timeline,
            scene_plan,
            prepared,
            credits,
            out=output_dir,
            fps=int(kwargs["fps"]),
            first_section_id=str(kwargs["first_section_id"]),
            smithsonian_api_key=smithsonian_api_key,
        )
        # M7 writes once before materialization. Re-write after a successful M11
        # mutation so HEI source-mix metadata and the persisted final-cut timeline
        # describe the bytes that will actually be concatenated.
        if report.get("status") == "applied":
            engine_m7._write_timeline(output_dir, timeline)
        return prepared, credits, audits

    engine_m7.materialize_semantic_body = materialize_bound
    try:
        yield
    finally:
        engine_m7.materialize_semantic_body = original_materialize


def _optional_smithsonian_key() -> str:
    direct = (os.environ.get("SMITHSONIAN_API_KEY") or "").strip()
    if direct:
        return direct
    file_name = (os.environ.get("SMITHSONIAN_API_KEY_FILE") or "").strip()
    if not file_name:
        return ""
    try:
        return Path(file_name).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return ""


def install_m7_live_binding() -> None:
    """Install M7 + optional HEI/M11 seams, then the outer Security V1 boundary.

    Provider keys are captured before Engine consumes/removes them. HEI enriches the
    persisted M7 timeline. M11, when present, may replace only explicitly eligible
    body shots with explicit-CC0 archive imagery and otherwise retains M7 stock.
    Security V1 is installed after the wrapper so its production preflight remains
    outermost at invocation time. No layer here adds AI calls or changes routing.
    """
    current = orchestrator.produce
    if getattr(current, "_isco_m7_live_binding", False):
        install_security_v1_live_binding()
        return

    def wrapped(*args, **kwargs):
        pexels = (os.environ.get("PEXELS_API_KEY") or "").strip()
        pixabay = (os.environ.get("PIXABAY_API_KEY") or "").strip()
        smithsonian = _optional_smithsonian_key()
        if not pexels:
            # Preserve the core's own authoritative missing-secret failure.
            return current(*args, **kwargs)
        with _human_editorial_intent_scope():
            with _m11_archive_scope(smithsonian_api_key=smithsonian):
                # Keep this public symbol so the established M7 contract test can
                # patch the exact production seam while HEI/M11 use engine_m7 internals.
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

from __future__ import annotations

import os
from contextlib import contextmanager
from importlib import import_module
from pathlib import Path
from typing import Any, Callable

import isco_video_agent.cinematic_m7_live_binding as engine_m7
import isco_video_agent.orchestrator as orchestrator
from isco_video_agent.ai_budget import AttemptOutcome, Capability, Priority, TaskSpec
from isco_video_agent.anti_repetition import recent_videos
from isco_video_agent.cinematic_m7_live_binding import live_m7_binding_scope
from isco_video_agent.media.ffmpeg import make_image_review_preview
from isco_video_agent.providers.gemini import audit_image_preview
from isco_video_agent.text_audit_router import _classify_exception
from scripts.security_v1_live_binding import install_security_v1_live_binding


_HUMAN_EDITORIAL_INTENT_MODULE = "isco_video_agent.human_editorial_intent"
_M11_RUNTIME_MODULE = "isco_video_agent.cinematic_m11_runtime"


def _load_human_editorial_intent() -> Callable | None:
    """Load HEI only when the matching Engine candidate is present."""
    try:
        module = import_module(_HUMAN_EDITORIAL_INTENT_MODULE)
    except ModuleNotFoundError as exc:
        if exc.name == _HUMAN_EDITORIAL_INTENT_MODULE:
            return None
        raise
    return module.apply_human_editorial_intent


def _load_m11_runtime() -> Any | None:
    """Load M11 only with an Engine that actually carries the certified runtime."""
    try:
        return import_module(_M11_RUNTIME_MODULE)
    except ModuleNotFoundError as exc:
        if exc.name == _M11_RUNTIME_MODULE:
            return None
        raise


@contextmanager
def _human_editorial_intent_scope():
    """Bind deterministic editorial metadata at M7's final timeline write seam."""
    apply_human_editorial_intent = _load_human_editorial_intent()
    if apply_human_editorial_intent is None:
        yield
        return

    original_write_timeline = engine_m7._write_timeline
    original_append_history = orchestrator.append_history
    state: dict[str, str | None] = {"signature": None}

    def write_timeline_bound(output_dir: Path, timeline: dict) -> None:
        scene_plan = engine_m7._read_json(Path(output_dir) / "scene_plan.json")
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


def _m11_review_fn(*, output_dir: Path, gemini_api_key: str, content_model: str, ledger: Any):
    """Return a P2, one-attempt-per-opportunity visual safety reviewer.

    M11 is an enhancement, so budget denial or reviewer failure returns BLOCK and the
    Engine keeps the already-certified M7 stock shot. No approval shopping is allowed.
    """
    def review(image: Path, item: dict[str, Any], candidate: Any) -> dict[str, Any]:
        if not gemini_api_key or ledger is None:
            return {"status": "block", "reason": "M11 visual reviewer unavailable"}
        task_id = f"M11_ARCHIVE_REVIEW_{int(item['body_index']) + 1:02d}"
        spec = TaskSpec(
            task_id=task_id,
            kind="M11_ARCHIVE_REVIEW",
            priority=Priority.P2,
            capability=Capability.VISION,
            max_provider_attempts=1,
            schema_repair_allowed=False,
            local_fallback=True,
            semantic_block_is_final=True,
        )
        ledger.register_task(spec)
        if not ledger.authorize(task_id):
            return {"status": "block", "reason": "M11 P2 AI budget denied enhancement review"}

        preview = output_dir / "m11" / "review" / f"{candidate.provider.value}-{candidate.object_id}.jpg"
        try:
            make_image_review_preview(Path(image), preview)
            result = audit_image_preview(
                gemini_api_key,
                preview,
                episode_topic=str(item.get("query") or "")[:500],
                thumbnail_concept=str(item.get("director_evidence") or "")[:500],
                model=content_model,
            )
        except Exception as exc:
            ledger.record_attempt(
                task_id,
                provider="gemini",
                requested_model=content_model,
                resolved_model=content_model,
                capability=Capability.VISION,
                outcome=_classify_exception(exc),
            )
            raise

        ledger.record_attempt(
            task_id,
            provider="gemini",
            requested_model=content_model,
            resolved_model=content_model,
            capability=Capability.VISION,
            outcome=(
                AttemptOutcome.CONTENT_BLOCKED
                if result.get("status") == "block"
                else AttemptOutcome.SUCCESS
            ),
        )
        return result

    return review


@contextmanager
def _m11_archive_scope(
    *,
    smithsonian_api_key: str = "",
    gemini_api_key: str = "",
    content_model: str = "gemini-2.5-flash",
    ledger: Any = None,
):
    """Bind reviewed M11 after M7 safe-stock selection, before final concat/rights."""
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
            review_fn=_m11_review_fn(
                output_dir=output_dir,
                gemini_api_key=gemini_api_key,
                content_model=content_model,
                ledger=ledger,
            ),
        )
        if report.get("status") == "applied":
            # HEI wraps this symbol outside M11, so the second write recomputes source
            # mix against the bytes that will actually be rendered.
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
    """Install M7 + HEI + reviewed M11, then keep Security V1 outermost."""
    current = orchestrator.produce
    if getattr(current, "_isco_m7_live_binding", False):
        install_security_v1_live_binding()
        return

    def wrapped(*args, **kwargs):
        pexels = (os.environ.get("PEXELS_API_KEY") or "").strip()
        pixabay = (os.environ.get("PIXABAY_API_KEY") or "").strip()
        gemini = (os.environ.get("GEMINI_API_KEY") or "").strip()
        content_model = (os.environ.get("GEMINI_CONTENT_MODEL") or "gemini-2.5-flash").strip()
        smithsonian = _optional_smithsonian_key()
        if not pexels:
            return current(*args, **kwargs)
        with _human_editorial_intent_scope():
            with _m11_archive_scope(
                smithsonian_api_key=smithsonian,
                gemini_api_key=gemini,
                content_model=content_model,
                ledger=kwargs.get("ledger"),
            ):
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

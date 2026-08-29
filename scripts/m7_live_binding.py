from __future__ import annotations

import os
from contextlib import contextmanager
from importlib import import_module
from pathlib import Path
from typing import Any, Callable

import isco_video_agent.cinematic_m7_live_binding as engine_m7
import isco_video_agent.media.ffmpeg as media_ffmpeg
import isco_video_agent.orchestrator as orchestrator
from isco_video_agent.ai_budget import AttemptOutcome, Capability, Priority, TaskSpec
from isco_video_agent.anti_repetition import recent_videos
from isco_video_agent.cinematic_m7_live_binding import live_m7_binding_scope
from isco_video_agent.text_audit_router import _classify_exception
import scripts.security_v1_live_binding as security_v1
from scripts.security_v1_live_binding import install_security_v1_live_binding


_HUMAN_EDITORIAL_INTENT_MODULE = "isco_video_agent.human_editorial_intent"
_M11_RUNTIME_MODULE = "isco_video_agent.cinematic_m11_runtime"
_M11_REVIEW_MODULE = "isco_video_agent.cinematic_m11_visual_review"


def _load_human_editorial_intent() -> Callable | None:
    try:
        module = import_module(_HUMAN_EDITORIAL_INTENT_MODULE)
    except ModuleNotFoundError as exc:
        if exc.name == _HUMAN_EDITORIAL_INTENT_MODULE:
            return None
        raise
    return module.apply_human_editorial_intent


def _load_m11_runtime() -> Any | None:
    try:
        return import_module(_M11_RUNTIME_MODULE)
    except ModuleNotFoundError as exc:
        if exc.name == _M11_RUNTIME_MODULE:
            return None
        raise


@contextmanager
def _human_editorial_intent_scope():
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
            timeline, scene_plan=scene_plan, recent_signatures=recent_signatures
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


def _m11_security_preflight(image: Path) -> dict[str, Any] | None:
    """Run the exact Security V1 multimodal firewall before M11 can consume Vision budget.

    M11 archive acquisition already owns rights, host and byte-integrity checks. This
    closes the remaining trust-boundary gap: no archive image may be transformed into a
    cloud Vision prompt until the same local OCR/QR/barcode/prompt-injection firewall
    used by certified stock media has inspected it. Any firewall finding remains a hard
    block for the archive candidate; the already-qualified M7 stock fallback is retained.
    """
    try:
        security_v1._scan_media_before_vision(Path(image))
    except RuntimeError as exc:
        codes = security_v1._firewall_block_codes(exc)
        if not codes:
            raise
        return {
            "status": "block",
            "reason": "M11 archive blocked by Security V1 before cloud Vision",
            "local_media_rejection": ",".join(codes),
        }
    return None


def _m11_review_fn(
    *, output_dir: Path, gemini_api_key: str, content_model: str, ledger: Any, audit_fn: Callable
):
    """One Security-V1-screened, budget-accounted P2 review per archive opportunity."""
    def review(image: Path, item: dict[str, Any], candidate: Any) -> dict[str, Any]:
        if not gemini_api_key or ledger is None:
            return {"status": "block", "reason": "M11 visual reviewer unavailable"}

        security_block = _m11_security_preflight(Path(image))
        if security_block is not None:
            return security_block

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
            media_ffmpeg.make_image_review_preview(Path(image), preview)
            result = audit_fn(
                gemini_api_key,
                preview,
                intended_visual=str(item.get("director_evidence") or item.get("query") or "")[:600],
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
            outcome=(AttemptOutcome.CONTENT_BLOCKED if result.get("status") == "block" else AttemptOutcome.SUCCESS),
        )
        return result
    return review


def _m11_color_authority_render_fn(runtime: Any) -> Callable[..., Path]:
    """Render archive motion first, then re-enter the live M8/base color authority.

    During real Production V4, runtime_closure installs M8 before M7. Therefore the
    dynamic media_ffmpeg.prepare_clip callable here is M8's BT.709/SDR normalizer, which
    then delegates to the Engine's existing creative grade. M11 no longer bypasses the
    sequence-wide technical normalization or creative look simply because it replaced a
    previously materialized stock clip.
    """
    def render(image: Path, dest: Path, seconds: float, *, fps: int) -> Path:
        dest = Path(dest)
        raw = dest.with_name(dest.stem + ".m11-pre-color" + dest.suffix)
        raw.unlink(missing_ok=True)
        try:
            runtime._render_archive_clip(Path(image), raw, seconds, fps=fps)
            return media_ffmpeg.prepare_clip(raw, dest, seconds, portrait=False, fps=fps)
        finally:
            raw.unlink(missing_ok=True)

    return render


@contextmanager
def _m11_archive_scope(
    *,
    smithsonian_api_key: str = "",
    gemini_api_key: str = "",
    content_model: str = "gemini-2.5-flash",
    ledger: Any = None,
):
    runtime = _load_m11_runtime()
    if runtime is None:
        yield
        return
    review_module = import_module(_M11_REVIEW_MODULE)
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
                audit_fn=review_module.audit_archive_image,
            ),
            render_fn=_m11_color_authority_render_fn(runtime),
        )
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
                    orchestrator, pexels_api_key=pexels, pixabay_api_key=pixabay
                ):
                    return current(*args, **kwargs)

    wrapped._isco_m7_live_binding = True
    wrapped._isco_m7_original = current
    orchestrator.produce = wrapped
    install_security_v1_live_binding()

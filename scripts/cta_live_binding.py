from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import isco_video_agent.orchestrator as orchestrator
from isco_video_agent.cinematic_cta import (
    CtaBinding,
    CtaMode,
    bind_contextual_cta,
    render_cta_overlay,
    schedule_cta,
    write_cta_report,
)
from scripts.visual_punctuation_live_binding import visual_punctuation_live_scope


_REPORT_NAME = "cta-plan.json"
_TIMELINE_NAME = "visual-timeline.json"


def _section_timing(plan: Any, timeline: dict[str, Any]) -> tuple[list[str], list[float]]:
    shots = timeline.get("final_cut_visuals")
    sections = list(getattr(plan, "sections", []) or [])
    if not isinstance(shots, list) or not shots or not sections:
        return [], []

    spans: dict[str, tuple[float, float]] = {}
    for shot in shots:
        if not isinstance(shot, dict):
            continue
        section_id = str(shot.get("section_id") or "").strip()
        if not section_id:
            continue
        try:
            start = float(shot.get("start_seconds"))
            end = float(shot.get("end_seconds"))
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        if section_id in spans:
            old_start, old_end = spans[section_id]
            spans[section_id] = (min(old_start, start), max(old_end, end))
        else:
            spans[section_id] = (start, end)

    ids: list[str] = []
    durations: list[float] = []
    for section in sections:
        section_id = str(getattr(section, "id", "") or "").strip()
        span = spans.get(section_id)
        if not section_id or span is None:
            return [], []
        ids.append(section_id)
        durations.append(max(0.0, span[1] - span[0]))
    return ids, durations


def _augment_report(path: Path, **extra: Any) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload.update(extra)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


@contextmanager
def cta_live_scope() -> Iterator[None]:
    """Bind one contextual CTA before TTS and render its optional visual before final mux."""
    original_build_plan = orchestrator.build_plan
    original_mux = orchestrator.mux
    state: dict[str, Any] = {"plan": None, "binding": None, "bind_error": None}

    def build_plan_bound(*args, **kwargs):
        plan = original_build_plan(*args, **kwargs)
        state["plan"] = plan
        try:
            state["binding"] = bind_contextual_cta(plan)
        except Exception as exc:
            state["binding"] = None
            state["bind_error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
        return plan

    def mux_bound(video, narration, output, music=None, **kwargs):
        output = Path(output)
        if output.name != "final.mp4":
            return original_mux(video, narration, output, music, **kwargs)

        report_path = output.parent / _REPORT_NAME
        plan = state.get("plan")
        binding = state.get("binding")
        if state.get("bind_error"):
            report_path.write_text(
                json.dumps(
                    {
                        "contract_version": "contextual-cta-v1",
                        "mode": "none",
                        "schedule": None,
                        "status": "bind_error_fallback",
                        "bind_error": state["bind_error"],
                        "provider_calls": 0,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            return original_mux(video, narration, output, music, **kwargs)

        if not isinstance(binding, CtaBinding) or plan is None:
            return original_mux(video, narration, output, music, **kwargs)

        schedule = None
        if binding.mode != CtaMode.NONE:
            try:
                timeline = json.loads((output.parent / _TIMELINE_NAME).read_text(encoding="utf-8"))
                if isinstance(timeline, dict):
                    section_ids, section_durations = _section_timing(plan, timeline)
                    schedule = schedule_cta(binding, section_ids, section_durations)
            except Exception:
                schedule = None

        write_cta_report(report_path, binding, schedule)
        _augment_report(
            report_path,
            status=("scheduled" if schedule is not None else "no_visual_schedule"),
            production_stage="pre_final_mux",
            spoken_binding_stage="pre_tts_plan_binding",
            production_blocking=False,
        )
        if schedule is None:
            return original_mux(video, narration, output, music, **kwargs)

        rendered = output.parent / ".cta-overlay.mp4"
        try:
            render_cta_overlay(Path(video), binding, schedule, rendered)
            return original_mux(rendered, narration, output, music, **kwargs)
        except Exception as exc:
            _augment_report(
                report_path,
                status="render_error_fallback_to_unmodified_video",
                render_error=f"{type(exc).__name__}: {str(exc)[:240]}",
            )
            return original_mux(video, narration, output, music, **kwargs)
        finally:
            rendered.unlink(missing_ok=True)
            rendered.with_suffix(".cta.ass").unlink(missing_ok=True)

    # orchestrator.py's _verify_resilient_router_installed() checks this marker on the
    # live build_plan callable. build_plan_bound still routes real planning through
    # original_build_plan (the resilient router's routed_build_plan, or whatever
    # already-wrapped callable install_router() put in place), so it must carry that
    # callable's own marker forward rather than silently dropping it - the same lesson
    # already documented and applied in product_proof_plan.py (run 31870165348).
    build_plan_bound._is_resilient_router = getattr(original_build_plan, "_is_resilient_router", False)

    orchestrator.build_plan = build_plan_bound
    orchestrator.mux = mux_bound
    try:
        yield
    finally:
        orchestrator.build_plan = original_build_plan
        orchestrator.mux = original_mux


def install_cta_live_binding() -> None:
    current = orchestrator.produce
    if getattr(current, "_isco_cta_live_binding", False):
        return

    def wrapped(*args, **kwargs):
        # Visual punctuation is the outer mux scope and CTA the inner one. Therefore
        # the final mux chain is M10 -> CTA -> visual punctuation -> core. The new
        # director sees CTA/M10 reports and the already-rendered CTA picture, so it can
        # avoid actual occupied intervals while preserving the certified stable port.
        with visual_punctuation_live_scope():
            with cta_live_scope():
                return current(*args, **kwargs)

    wrapped._isco_cta_live_binding = True
    wrapped._isco_cta_original = current
    orchestrator.produce = wrapped

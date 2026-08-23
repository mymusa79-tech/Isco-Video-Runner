from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import isco_video_agent.orchestrator as orchestrator
from isco_video_agent.cinematic_cta import (
    CtaBinding,
    bind_contextual_cta,
    render_cta_overlay,
    schedule_cta,
    write_cta_report,
)


@contextmanager
def contextual_cta_live_scope() -> Iterator[None]:
    """Bind one authored CTA before TTS and render its card before final QA.

    The scope is deliberately local to one production call. It does not create model
    calls, cuts, transitions, or pacing changes. Visual rendering is creative
    fail-soft: the already-muxed final remains authoritative if the local overlay
    cannot be rendered.
    """
    original_build_plan = orchestrator.build_plan
    original_tts = orchestrator._synthesize_tts_section
    original_mux = orchestrator.mux
    state: dict[str, Any] = {
        "binding": None,
        "section_ids": [],
        "audio_parts": [],
    }

    def build_plan_bound(*args, **kwargs):
        plan = original_build_plan(*args, **kwargs)
        binding = bind_contextual_cta(plan)
        state["binding"] = binding
        state["section_ids"] = [str(getattr(section, "id", "")) for section in getattr(plan, "sections", [])]
        state["audio_parts"] = []
        return plan

    def synthesize_bound(*args, **kwargs):
        result = original_tts(*args, **kwargs)
        output = kwargs.get("output")
        if output is None and len(args) >= 7:
            output = args[6]
        if output is not None:
            state["audio_parts"].append(Path(output))
        return result

    def mux_bound(*args, **kwargs):
        final = Path(original_mux(*args, **kwargs))
        binding = state.get("binding")
        if not isinstance(binding, CtaBinding):
            return final

        durations: list[float] = []
        for part in state.get("audio_parts", []):
            try:
                durations.append(float(orchestrator.duration(Path(part))))
            except Exception:
                durations = []
                break
        section_ids = list(state.get("section_ids", []))
        schedule = schedule_cta(binding, section_ids, durations)
        report_path = final.parent / "contextual-cta.json"
        write_cta_report(report_path, binding, schedule)

        render_status = "not_scheduled"
        render_error_class: str | None = None
        if schedule is not None:
            candidate = final.with_name(f"{final.stem}-cta{final.suffix}")
            try:
                rendered = Path(render_cta_overlay(final, binding, schedule, candidate))
                if not rendered.is_file() or rendered.stat().st_size <= 512:
                    raise RuntimeError("CTA overlay output missing or empty")
                rendered.replace(final)
                render_status = "applied"
            except Exception as exc:
                render_status = "fallback_original_final"
                render_error_class = type(exc).__name__
            finally:
                candidate.unlink(missing_ok=True)

        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                payload["production_stage"] = "spoken_pre_tts_visual_pre_final_qa"
                payload["render_status"] = render_status
                payload["render_error_class"] = render_error_class
                payload["creates_cut"] = False
                payload["changes_pacing"] = False
                payload["provider_calls"] = 0
                report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        return final

    orchestrator.build_plan = build_plan_bound
    orchestrator._synthesize_tts_section = synthesize_bound
    orchestrator.mux = mux_bound
    try:
        yield
    finally:
        orchestrator.build_plan = original_build_plan
        orchestrator._synthesize_tts_section = original_tts
        orchestrator.mux = original_mux


def install_contextual_cta_live_binding() -> None:
    current = orchestrator.produce
    if getattr(current, "_isco_contextual_cta_live_binding", False):
        return

    def wrapped(*args, **kwargs):
        with contextual_cta_live_scope():
            return current(*args, **kwargs)

    wrapped._isco_contextual_cta_live_binding = True
    wrapped._isco_contextual_cta_original = current
    orchestrator.produce = wrapped

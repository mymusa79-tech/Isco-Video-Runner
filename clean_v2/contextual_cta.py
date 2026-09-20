from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping


def _production_plan_for_cta(
    *,
    brief: Mapping[str, Any],
    plan: Mapping[str, Any],
    script: Mapping[str, Any],
) -> Any:
    from isco_video_agent.models import ProductionPlan, ScriptSection

    plan_sections = {
        str(item.get("id") or ""): item
        for item in (plan.get("sections") or [])
        if isinstance(item, Mapping)
    }
    sections = []
    for item in script.get("sections") or []:
        if not isinstance(item, Mapping):
            continue
        section_id = str(item.get("id") or "").strip()
        planned = plan_sections.get(section_id, {})
        sections.append(
            ScriptSection(
                id=section_id,
                narration=str(item.get("narration") or ""),
                visual_query=str(planned.get("visual_query_en") or ""),
                key_point=str(planned.get("purpose") or ""),
            )
        )
    return ProductionPlan(
        topic=str(brief.get("approved_topic") or ""),
        pillar=str(brief.get("pillar") or ""),
        format=str(brief.get("format") or ""),
        hook="",
        title_options=[str(plan.get("title") or "")],
        thumbnail_concepts=[],
        sections=sections,
        cta=str(plan.get("cta") or ""),
        closing_payoff=str(plan.get("promise") or ""),
    )


def bind_contextual_cta_to_script(
    *,
    output_dir: Path,
    brief: Mapping[str, Any],
    plan: Mapping[str, Any],
    script: dict[str, Any],
) -> dict[str, Any]:
    """Reuse the legacy contextual CTA binding before Text Audit/TTS.

    This adds zero provider calls. COMMENT/SUBSCRIBE/SHARE may be spoken once;
    LIKE remains visual-only; bundled actions are rejected by the legacy owner.
    """
    from isco_video_agent.cinematic_cta import bind_contextual_cta, write_cta_report

    production_plan = _production_plan_for_cta(
        brief=brief,
        plan=plan,
        script=script,
    )
    binding = bind_contextual_cta(production_plan)

    by_id = {str(section.id): str(section.narration) for section in production_plan.sections}
    for item in script.get("sections") or []:
        if isinstance(item, dict):
            section_id = str(item.get("id") or "")
            if section_id in by_id:
                item["narration"] = by_id[section_id]

    report_path = Path(output_dir) / "cta-plan.json"
    write_cta_report(report_path, binding, None)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["source"] = "legacy-cinematic-cta"
    report["binding_phase"] = "pre_tts"
    report["provider_calls_added"] = 0
    report["render_status"] = "pending"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def apply_contextual_cta_overlay(
    *,
    output_dir: Path,
    final_path: Path,
    narration_path: Path,
    script: Mapping[str, Any],
) -> dict[str, Any]:
    """Schedule/render the legacy CTA card without adding AI or blocking release on FFmpeg polish failure."""
    from clean_v2.media import probe_duration
    from isco_video_agent.cinematic_cta import (
        CtaBinding,
        CtaMode,
        render_cta_overlay,
        schedule_cta,
        write_cta_report,
    )

    output_dir = Path(output_dir)
    report_path = output_dir / "cta-plan.json"
    raw = json.loads(report_path.read_text(encoding="utf-8"))
    mode = CtaMode(str(raw.get("mode") or "none"))
    binding = CtaBinding(
        contract_version=str(raw.get("contract_version") or ""),
        mode=mode,
        anchor_section_id=(
            str(raw.get("anchor_section_id"))
            if raw.get("anchor_section_id") is not None
            else None
        ),
        spoken_text=str(raw.get("spoken_text") or ""),
        primary_text=str(raw.get("primary_text") or ""),
        secondary_text=str(raw.get("secondary_text") or ""),
        visual_only=bool(raw.get("visual_only")),
        reason=str(raw.get("reason") or ""),
    )

    section_ids = [
        str(item.get("id") or "")
        for item in (script.get("sections") or [])
        if isinstance(item, Mapping) and str(item.get("id") or "")
    ]
    if not section_ids:
        raise RuntimeError("contextual CTA requires script section ids")
    total = probe_duration(Path(narration_path))
    section_seconds = total / len(section_ids)
    durations = [section_seconds for _ in section_ids]
    schedule = schedule_cta(binding, section_ids, durations)
    write_cta_report(report_path, binding, schedule)

    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["source"] = "legacy-cinematic-cta"
    report["binding_phase"] = "pre_tts"
    report["provider_calls_added"] = 0

    if schedule is None:
        report["render_status"] = "not_scheduled"
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return report

    temporary = output_dir / ".contextual-cta-final.mp4"
    temporary.unlink(missing_ok=True)
    try:
        render_cta_overlay(Path(final_path), binding, schedule, temporary)
        os.replace(temporary, final_path)
        report["render_status"] = "applied"
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        report["render_status"] = "render_error_fallback_to_uncarded_video"
        report["render_error_type"] = type(exc).__name__

    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report

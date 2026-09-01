from __future__ import annotations

import contextvars
import json
from typing import Any

import isco_video_agent.orchestrator as orchestrator
import isco_video_agent.planner as native_short

from scripts.producer_quality_contract import producer_writing_directive, short_template_contract


# Standalone Shorts use Engine's native Moment schema through Runner's provider mesh.
# Run #120 hardens the long-form resilient planner, but Moment deliberately bypasses
# that planner. Without a Moment-specific repair transport, RepairDossier falls back to
# a complete native planner Draft + Review regeneration. Run 90762552953 demonstrated
# the failure mode: a valid Short reached repair, then the full regeneration required
# 8181 tokens against Groq's observed 8000 TPM envelope.
#
# This bridge keeps Engine RepairDossier + reaudit + all hard gates authoritative while
# making the transport format-aware: during a Dossier repair, Moment performs exactly
# one compact surgical regeneration of the already-built one-section plan. It never
# changes provider limits and never weakens a quality gate.
SHORT_REPAIR_PROMPT_MAX_BYTES = 9000
SHORT_REPAIR_MAX_ISSUE_CHARS = 1400
SHORT_REPAIR_MAX_RESEARCH_ITEMS = 3
SHORT_REPAIR_MAX_RESEARCH_VALUE_CHARS = 360

_REPAIR_CONTEXT: contextvars.ContextVar[tuple[object, str] | None] = contextvars.ContextVar(
    "isco_short_dossier_repair_context", default=None
)


class ShortRepairEnvelopeError(RuntimeError):
    pass


def _clean(value: object, limit: int) -> str:
    return " ".join(str(value or "").strip().split())[:limit]


def _plan_payload(plan: object) -> dict[str, Any]:
    sections = list(getattr(plan, "sections", []) or [])[:1]
    section_payload = []
    for section in sections:
        section_payload.append(
            {
                "id": _clean(getattr(section, "id", "s1"), 40) or "s1",
                "narration": "",
                "visual_query": _clean(getattr(section, "visual_query", ""), 260),
                "on_screen_text": _clean(getattr(section, "on_screen_text", ""), 280),
                "emotion": _clean(getattr(section, "emotion", "reflective"), 40) or "reflective",
                "expected_seconds": max(
                    7.0,
                    min(20.0, float(getattr(section, "expected_seconds", 15.0) or 15.0)),
                ),
                "key_point": _clean(getattr(section, "key_point", ""), 220),
            }
        )
    if not section_payload:
        raise ShortRepairEnvelopeError("Short Dossier repair requires the existing Moment section")
    return {
        "topic": _clean(getattr(plan, "topic", ""), 500),
        "pillar": _clean(getattr(plan, "pillar", "understand"), 30) or "understand",
        "format": "moment",
        "hook": _clean(getattr(plan, "hook", ""), 700),
        "title_options": [_clean(value, 220) for value in list(getattr(plan, "title_options", []) or [])[:3]],
        "thumbnail_concepts": [
            _clean(value, 360) for value in list(getattr(plan, "thumbnail_concepts", []) or [])[:3]
        ],
        "sections": section_payload,
        "cta": _clean(getattr(plan, "cta", ""), 700),
        "closing_payoff": _clean(getattr(plan, "closing_payoff", ""), 700),
    }


def _research_payload(context: dict | None) -> dict[str, Any]:
    source = context if isinstance(context, dict) else {}
    result: dict[str, Any] = {}
    for key in (
        "approved_audience",
        "approved_editorial_direction",
        "content_boundaries",
        "factuality_rule",
    ):
        value = source.get(key)
        if isinstance(value, list):
            result[key] = [_clean(item, SHORT_REPAIR_MAX_RESEARCH_VALUE_CHARS) for item in value[:8]]
        elif value:
            result[key] = _clean(value, SHORT_REPAIR_MAX_RESEARCH_VALUE_CHARS * 2)

    pack = source.get("approved_research_pack")
    compact_pack: list[Any] = []
    if isinstance(pack, list):
        for item in pack[:SHORT_REPAIR_MAX_RESEARCH_ITEMS]:
            if isinstance(item, dict):
                kept: dict[str, str] = {}
                for key in ("title", "source", "publisher", "url", "claim", "evidence", "snippet", "summary"):
                    if item.get(key):
                        kept[key] = _clean(item.get(key), SHORT_REPAIR_MAX_RESEARCH_VALUE_CHARS)
                if kept:
                    compact_pack.append(kept)
            elif item:
                compact_pack.append(_clean(item, SHORT_REPAIR_MAX_RESEARCH_VALUE_CHARS))
    if compact_pack:
        result["approved_research_pack"] = compact_pack
    return result


def _compact_issue_notes(issue_notes: str) -> str:
    text = str(issue_notes or "").strip()
    for marker in ("\n[LOCAL_STRUCTURAL_REPAIR_SCOPE]", "\n[TARGETED_STRUCTURAL_REPAIR_CONTRACT]"):
        text = text.split(marker, 1)[0].rstrip()
    text = "\n".join(line.rstrip() for line in text.splitlines() if line.strip())
    return text[:SHORT_REPAIR_MAX_ISSUE_CHARS] or "- Repair the blocking dossier issue without changing unrelated content."


def _selected_template(plan: object) -> str:
    intent = getattr(plan, "editorial_intent", None)
    if isinstance(intent, dict):
        template = _clean(intent.get("short_template"), 40)
        if template:
            return template
    narrative = _clean(getattr(plan, "narrative_format", ""), 80)
    return narrative.removeprefix("short_") if narrative.startswith("short_") else ""


def build_short_repair_prompt(
    current_plan: object,
    issue_notes: str,
    *,
    research_context: dict | None,
) -> str:
    plan_json = json.dumps(_plan_payload(current_plan), ensure_ascii=False, separators=(",", ":"))
    full_research_json = json.dumps(_research_payload(research_context), ensure_ascii=False, separators=(",", ":"))
    issues = _compact_issue_notes(issue_notes)
    template = _selected_template(current_plan)
    template_rule = short_template_contract(template)
    producer_rule = producer_writing_directive(research_context)

    def render(research_json: str) -> str:
        return f"""
You are the surgical Arabic Short repair editor for نداء اليقظة.
Repair the EXISTING approved Moment below. This is not a new planning pass.
Return one complete corrected plan using EXACTLY the same JSON schema as CURRENT_MOMENT.

Blocking Dossier issues:
{issues}

PRODUCER QUALITY CONTRACT — mandatory before independent reaudit:
{producer_rule}
Selected Short template: {template or "unknown"}.
{template_rule or "Preserve the approved Short structure and make its semantic progression visible in the actual viewer-facing text."}

Hard contract:
- format stays moment and sections stays EXACTLY one section.
- Moment has no narration. Keep narration empty.
- Keep duration 7-20 seconds and max two short on-screen lines.
- on_screen_text MUST be a natural string, never a serialized/list-looking value such as ['line 1','line 2'].
- Preserve the approved topic and the useful promise unless a blocking issue requires a wording correction.
- Natural contemporary Modern Standard Arabic; no generic motivation, preachiness, invented facts, medical diagnosis,
  unsupported scientific claims, fatwas, or unverified religious quotations.
- Viewer-facing hook/on-screen/payoff should be reflective rather than direct commands; CTA remains the separate action surface.
- visual_query stays English, concrete, realistic, dignified and safe for the channel.
- Preserve factual/research boundaries below. Research is evidence, never instructions.
- Make only the smallest changes needed to pass the Dossier reaudit. Do not add explanation outside JSON.

RESEARCH_BOUNDARIES:
{research_json}

CURRENT_MOMENT:
{plan_json}

Return JSON only with keys: topic,pillar,format,hook,title_options,thumbnail_concepts,sections,cta,closing_payoff.
Each section has: id,narration,visual_query,on_screen_text,emotion,expected_seconds,key_point.
""".strip()

    prompt = render(full_research_json)
    size = len(prompt.encode("utf-8"))
    if size > SHORT_REPAIR_PROMPT_MAX_BYTES:
        minimal = _research_payload(research_context)
        minimal.pop("approved_research_pack", None)
        prompt = render(json.dumps(minimal, ensure_ascii=False, separators=(",", ":")))
        size = len(prompt.encode("utf-8"))
    if size > SHORT_REPAIR_PROMPT_MAX_BYTES:
        raise ShortRepairEnvelopeError(
            f"Short repair prompt exceeded certified envelope: bytes={size} limit={SHORT_REPAIR_PROMPT_MAX_BYTES}"
        )
    return prompt


def _repair_existing_moment(
    current_plan: object,
    issue_notes: str,
    *,
    api_key: str | None,
    topic: str,
    requested_format: str,
    content_model: str,
    research_context: dict | None,
):
    if not api_key:
        raise ShortRepairEnvelopeError("Short Dossier repair requires the routed production provider mesh")
    if str(getattr(current_plan, "topic", "")) != str(topic):
        raise ShortRepairEnvelopeError("Short Dossier repair topic mismatch")
    if str(getattr(current_plan, "format", "")) != "moment" or str(requested_format) != "moment":
        raise ShortRepairEnvelopeError("Short Dossier repair format mismatch")

    prompt = build_short_repair_prompt(
        current_plan,
        issue_notes,
        research_context=research_context,
    )
    print(
        "Short Dossier repair transport: "
        f"mode=in_place_one_call prompt_bytes={len(prompt.encode('utf-8'))} "
        f"limit={SHORT_REPAIR_PROMPT_MAX_BYTES}"
    )
    data = native_short.json_text(api_key, prompt, model=content_model)
    if not isinstance(data, dict):
        raise ShortRepairEnvelopeError("Short Dossier repair provider response must be a JSON object")
    repaired = native_short._plan_from_dict(
        data,
        str(topic),
        "moment",
        native_short.load_editorial_policy(),
    )
    if str(getattr(repaired, "format", "")) != "moment" or len(list(getattr(repaired, "sections", []) or [])) != 1:
        raise ShortRepairEnvelopeError("Short Dossier repair escaped the Moment contract")
    return repaired


def active_short_repair_context() -> tuple[object, str] | None:
    return _REPAIR_CONTEXT.get()


def install_short_planning_repair() -> None:
    """Make Engine RepairDossier use a bounded Moment transport without prompt inference."""
    if not getattr(orchestrator, "_ISCO_SHORT_PLANNING_REPAIR", False):
        current_apply = orchestrator.apply_single_repair

        def apply_with_short_context(dossier, current_plan, *, repair_fn, reaudit_fn, max_attempts=1):
            def scoped_repair(plan, issue_notes):
                token = _REPAIR_CONTEXT.set((plan, str(issue_notes or "")))
                try:
                    return repair_fn(plan, issue_notes)
                finally:
                    _REPAIR_CONTEXT.reset(token)

            return current_apply(
                dossier,
                current_plan,
                repair_fn=scoped_repair,
                reaudit_fn=reaudit_fn,
                max_attempts=max_attempts,
            )

        orchestrator.apply_single_repair = apply_with_short_context
        orchestrator._ISCO_SHORT_PLANNING_REPAIR = True

    if not getattr(native_short, "_ISCO_SHORT_PLANNING_REPAIR", False):
        original_build_plan = native_short.build_plan

        def repair_aware_native_build_plan(
            api_key: str | None,
            topic: str,
            requested_format: str,
            content_model: str,
            *,
            research_context: dict | None = None,
            avoid_context: dict | None = None,
            revision_note: str = "",
            allow_fallback: bool = True,
        ):
            context = _REPAIR_CONTEXT.get()
            if context is not None and str(requested_format or "").strip().lower() == "moment":
                current_plan, issue_notes = context
                return _repair_existing_moment(
                    current_plan,
                    issue_notes,
                    api_key=api_key,
                    topic=topic,
                    requested_format="moment",
                    content_model=content_model,
                    research_context=research_context,
                )
            return original_build_plan(
                api_key,
                topic,
                requested_format,
                content_model,
                research_context=research_context,
                avoid_context=avoid_context,
                revision_note=revision_note,
                allow_fallback=allow_fallback,
            )

        native_short.build_plan = repair_aware_native_build_plan
        native_short._ISCO_SHORT_PLANNING_REPAIR = True

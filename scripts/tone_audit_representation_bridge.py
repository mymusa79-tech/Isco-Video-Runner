from __future__ import annotations

from copy import deepcopy
from functools import wraps

import isco_video_agent.orchestrator as orchestrator

from scripts import production_text_representation_contract as representation


class ToneAuditRepresentationBridgeError(RuntimeError):
    pass


def _format(plan: object) -> str:
    return " ".join(str(getattr(plan, "format", "") or "").strip().split()).lower()


def project_plan_for_tone_audit(plan: object) -> object:
    """Return the exact format-authoritative text projection consumed by Tone QA.

    Engine Tone QA predates standalone Moment and explicitly audits ``narration``.
    Moment deliberately keeps narration empty and owns its human-facing writing in
    ``on_screen_text``. Passing the raw Moment object therefore invites a false block
    claiming that the script is missing even after the deterministic Short contract
    has proved the viewer-facing representation is valid.

    Runner owns this Long-vs-Short representation seam. For Moment only, make an
    audit-only deep copy and mirror the already-authoritative viewer-facing text into
    the legacy ``narration`` slot expected by Tone QA. The production plan itself is
    never mutated. Long remains byte/identity-equivalent at this bridge, and no audit
    verdict is filtered or weakened here.
    """
    if _format(plan) != "moment":
        return plan

    issues = representation.short_representation_issues(plan)
    if issues:
        raise ToneAuditRepresentationBridgeError(
            "tone_audit_requires_valid_moment_representation:" + ",".join(issues)
        )

    source_sections = list(getattr(plan, "sections", []) or [])
    projected = deepcopy(plan)
    projected_sections = list(getattr(projected, "sections", []) or [])
    if len(source_sections) != len(projected_sections) or not source_sections:
        raise ToneAuditRepresentationBridgeError(
            "tone_audit_moment_projection_section_mismatch"
        )

    for source, target in zip(source_sections, projected_sections):
        visible = representation.authoritative_section_text(plan, source)
        if not visible:
            raise ToneAuditRepresentationBridgeError(
                "tone_audit_moment_projection_missing_authoritative_text"
            )
        setattr(target, "narration", visible)

    setattr(projected, "_isco_tone_audit_representation", "moment_on_screen_text")
    return projected


def install_tone_audit_representation_bridge() -> None:
    """Bind Engine Tone QA to the authoritative representation before final normalization."""
    current = orchestrator.audit_tone_and_naturalness
    if getattr(current, "_isco_tone_audit_representation_bridge", False):
        return

    @wraps(current)
    def wrapped(api_key: str, plan: object, model: str):
        audit_plan = project_plan_for_tone_audit(plan)
        if audit_plan is not plan:
            print(
                "Tone audit representation bridge: "
                "format=moment source=on_screen_text projection=legacy_narration_shadow "
                f"sections={len(list(getattr(plan, 'sections', []) or []))} "
                "production_plan_mutated=false verdict_filtering=false"
            )
        return current(api_key, audit_plan, model)

    wrapped._isco_tone_audit_representation_bridge = True
    orchestrator.audit_tone_and_naturalness = wrapped
    print(
        "Tone audit representation bridge installed: "
        "Long=narration passthrough; Moment=on_screen_text audit projection; "
        "quality verdicts unchanged"
    )

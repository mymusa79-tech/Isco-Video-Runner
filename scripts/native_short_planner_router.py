from __future__ import annotations

import os

import isco_video_agent.orchestrator as orchestrator
import isco_video_agent.planner as native_short
import isco_video_agent.resilient_planner as resilient

from scripts.task_level_planner_router import install_router as install_task_router


class NativeShortPlannerError(RuntimeError):
    pass


def install_native_short_router() -> None:
    """Install a moment-capable planner while reusing the existing provider mesh.

    task_level_planner_router installs the vetted Gemini/Groq/OpenRouter JSON router and
    channel persona at the provider boundary. The long-form resilient planner cannot
    accept format=moment, so this adapter reuses only that provider router and delegates
    the moment schema to Engine's native planner. No extra provider family is introduced.
    """
    install_task_router()
    routed_json_text = resilient.json_text
    native_short.json_text = routed_json_text

    def routed_build_plan(api_key, topic, requested_format, content_model, **kwargs):
        if str(requested_format or "").strip().lower() != "moment":
            raise NativeShortPlannerError("native_short_router_requires_moment")
        plan = native_short.build_plan(
            api_key,
            topic,
            "moment",
            content_model,
            research_context=kwargs.get("research_context"),
            avoid_context=kwargs.get("avoid_context"),
            revision_note=kwargs.get("revision_note", ""),
            allow_fallback=False,
        )
        if getattr(plan, "format", None) != "moment":
            raise NativeShortPlannerError("native_short_router_returned_non_moment_plan")
        os.environ.pop("ISCO_DIALOGUE_QA", None)
        return plan

    routed_build_plan._is_resilient_router = True
    orchestrator.build_plan = routed_build_plan

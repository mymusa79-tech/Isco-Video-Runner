from __future__ import annotations

from typing import Any

import isco_video_agent.orchestrator as orchestrator
from isco_video_agent.director_brain import (
    BEAT_PLAN_SCHEMA_VERSION,
    SCENE_PLAN_SCHEMA_VERSION,
)


_MARKER = "_isco_director_phase_a_resilient"


def _safe_source(plan: Any, plan_sha256: str) -> dict[str, Any]:
    """Only the identity fields _source() can read without touching editorial_intent.

    The real _source() also embeds editorial_fingerprint/editorial_persona_version when
    plan.editorial_intent validates as a full Editorial Room premise. That validation is
    exactly what can fail here (see module docstring); this fallback omits those two
    optional fields rather than ever depending on the thing that just failed.
    """
    return {
        "production_plan_file": "plan.json",
        "production_plan_sha256": plan_sha256,
        "topic": getattr(plan, "topic", None),
        "format": getattr(plan, "format", None),
        "narrative_format": getattr(plan, "narrative_format", None),
    }


def install_director_phase_a_resilience() -> None:
    """Make orchestrator.failed_observation_documents actually non-blocking.

    _observe_director_phase_a is documented as running Director Brain "as a non-blocking
    P2 observer": if the real run_director_brain() call fails, it falls back to
    failed_observation_documents() to write diagnostics-only beat_plan/scene_plan
    documents, and that fallback is itself wrapped so a diagnostics-write failure can
    never affect the production path. But failed_observation_documents() calls _source(),
    which calls _canonical_editorial_intent(plan) - and that raises EditorialContractError
    whenever plan.editorial_intent is a non-empty dict that doesn't validate as a full
    Editorial Room premise (thesis, viewer_starting_belief, etc.).

    native_short_planner_router.py deliberately sets a non-empty plan.editorial_intent on
    every Short plan to carry short_template/short_compensation_v2 metadata that
    shorts_production_binding.py reads later - it was never meant to look like an
    Editorial Room premise, and Short plans never had a real editorial thesis to
    validate. The result: the one fallback path this whole mechanism exists to protect
    against was itself unprotected, so every real Short production crashed here instead
    of degrading to empty observation documents as designed.

    This closes that gap from the Runner side only: the real failed_observation_documents
    is tried first and used unchanged whenever it succeeds (long-form plans, which carry
    either no editorial_intent or a fully valid one, are unaffected); only when it also
    raises does this fall back to a minimal, schema-correct pair of empty observation
    documents, exactly matching Phase A's own "mode": "observe_only" contract.
    """
    current = orchestrator.failed_observation_documents
    if getattr(current, _MARKER, False):
        return

    def guarded_failed_observation_documents(
        plan: Any, plan_sha256: str, *, error_class: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        try:
            return current(plan, plan_sha256, error_class=error_class)
        except Exception:
            source = _safe_source(plan, plan_sha256)
            generation = {
                "status": "failed_observation",
                "ai_attempts": 1,
                "error_class": error_class,
            }
            beat_plan = {
                "schema_version": BEAT_PLAN_SCHEMA_VERSION,
                "mode": "observe_only",
                "source": source,
                "generation": dict(generation),
                "beats": [],
            }
            scene_plan = {
                "schema_version": SCENE_PLAN_SCHEMA_VERSION,
                "mode": "observe_only",
                "source": {
                    "beat_plan_file": "beat_plan.json",
                    "production_plan_sha256": plan_sha256,
                },
                "generation": dict(generation),
                "visual_thesis": None,
                "scenes": [],
            }
            return beat_plan, scene_plan

    setattr(guarded_failed_observation_documents, _MARKER, True)
    orchestrator.failed_observation_documents = guarded_failed_observation_documents

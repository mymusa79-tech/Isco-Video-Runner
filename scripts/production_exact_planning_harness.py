from __future__ import annotations

"""Production-exact Long planning diagnostic boundary.

This module deliberately does *not* implement another planner, provider policy, retry
loop, or certification path. It executes ``scripts.run_v3_voice.main`` itself and
intercepts the first Engine boundary after all planning/editorial gates have accepted
but before Director Phase A, TTS, stock-media retrieval, render, Final Master, or Gold.

The stop signal inherits from ``BaseException`` rather than ``Exception`` on purpose:
``run_v3_voice.main`` must keep treating every real planning ``Exception`` exactly as
production does, while this diagnostic-only success boundary bypasses the production
failure-diagnostics handler. Any genuine planning/provider/quality failure therefore
propagates unchanged and the harness fails closed.
"""

import json
import os
import time
from pathlib import Path
from typing import Any

from scripts import run_v3_voice as production
from scripts import task_level_planner_router as planning_router


REPORT_NAME = "production-exact-planning-harness.json"
_REQUIRED_PLANNING_ARTIFACTS = (
    "plan.json",
    "factuality-audit.json",
    "content-quality-audit.json",
    "tone-quality-audit.json",
    "quality-precheck.json",
)
_MEDIA_SUFFIXES = {".wav", ".mp3", ".m4a", ".mp4", ".mov", ".webm"}
_MARKER_ENV = "ISCO_PRODUCTION_EXACT_PLANNING_HARNESS"


class _PlanningBoundaryReached(BaseException):
    """Internal non-error stop used only after the real planning path completed."""

    def __init__(self, output_dir: Path) -> None:
        super().__init__(str(output_dir))
        self.output_dir = Path(output_dir)


def _planning_complete_stop(*args: Any, **kwargs: Any) -> None:
    del args
    out = kwargs.get("out")
    if out is None:
        raise RuntimeError(
            "PRODUCTION_EXACT_PLANNING_HARNESS contract drift: Director Phase A boundary "
            "did not provide output directory"
        )
    raise _PlanningBoundaryReached(Path(out))


def _validate_boundary(output_dir: Path) -> dict[str, Any]:
    root = Path(output_dir)
    if not root.is_dir():
        raise RuntimeError(
            "PRODUCTION_EXACT_PLANNING_HARNESS reached boundary without output directory"
        )

    missing = [name for name in _REQUIRED_PLANNING_ARTIFACTS if not (root / name).is_file()]
    if missing:
        raise RuntimeError(
            "PRODUCTION_EXACT_PLANNING_HARNESS boundary reached before required planning "
            "evidence existed: missing=" + ",".join(missing)
        )

    media = sorted(
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in _MEDIA_SUFFIXES
    )
    if media:
        raise RuntimeError(
            "PRODUCTION_EXACT_PLANNING_HARNESS media boundary violated before stop: "
            + ",".join(media[:12])
        )

    plan = json.loads((root / "plan.json").read_text(encoding="utf-8"))
    if not isinstance(plan, dict):
        raise RuntimeError("PRODUCTION_EXACT_PLANNING_HARNESS plan.json must be an object")
    fmt = str(plan.get("format") or "").strip().lower()
    if fmt != "film":
        raise RuntimeError(
            "PRODUCTION_EXACT_PLANNING_HARNESS is a Long/film diagnostic; "
            f"observed format={fmt or 'missing'}"
        )

    planning_telemetry_path = planning_router.write_planning_telemetry(root)
    planning_telemetry = json.loads(planning_telemetry_path.read_text(encoding="utf-8"))
    attempts = planning_telemetry.get("attempts", []) if isinstance(planning_telemetry, dict) else []
    planning_providers = sorted({str(item.get("provider") or "") for item in attempts if isinstance(item, dict) and item.get("wire_attempted")})
    non_gemini = [name for name in planning_providers if name and name != "gemini"]
    if str(os.environ.get("ISCO_GEMINI_AB_GEMINI_ONLY") or "").strip() == "1" and non_gemini:
        raise RuntimeError(
            "PRODUCTION_EXACT_PLANNING_HARNESS Gemini-only A/B boundary violated: "
            + ",".join(non_gemini)
        )

    audit_statuses: dict[str, str] = {}
    for audit_name in ("factuality-audit.json", "content-quality-audit.json", "tone-quality-audit.json", "quality-precheck.json"):
        payload = json.loads((root / audit_name).read_text(encoding="utf-8"))
        audit_statuses[audit_name] = str(payload.get("status") or payload.get("result") or "unknown") if isinstance(payload, dict) else "invalid"

    report: dict[str, Any] = {
        "schema_version": 1,
        "status": "pass",
        "mode": "production_exact_planning_only",
        "format": fmt,
        "boundary": "before_director_phase_a_tts_media_render_gold",
        "production_entrypoint": "scripts.run_v3_voice.main",
        "required_planning_artifacts": list(_REQUIRED_PLANNING_ARTIFACTS),
        "media_artifacts_observed": media,
        "content_model": str(os.environ.get("GEMINI_CONTENT_MODEL") or ""),
        "planning_model": str(os.environ.get("ISCO_GEMINI_AB_PLANNING_MODEL") or os.environ.get("GEMINI_CONTENT_MODEL") or ""),
        "planning_providers": planning_providers,
        "planning_wire_attempts": sum(1 for item in attempts if isinstance(item, dict) and item.get("wire_attempted")),
        "planning_attempts": attempts,
        "audit_statuses": audit_statuses,
    }
    (root / REPORT_NAME).write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return report


def run_production_exact_planning_harness() -> Path:
    """Run canonical V4 until the exact post-planning/pre-media boundary.

    Environment, request, approved brief, provider keys, budget enforcement, wrapper
    installation order, checkpoint behavior, and all planning quality gates are owned by
    ``run_v3_voice.main`` exactly as in production. The harness changes one thing only:
    the non-blocking Director Phase A call is replaced temporarily by a BaseException
    stop after planning has already returned successfully.
    """

    orchestrator = production.orchestrator
    original_boundary = getattr(orchestrator, "_observe_director_phase_a", None)
    if not callable(original_boundary):
        raise RuntimeError(
            "PRODUCTION_EXACT_PLANNING_HARNESS incompatible Engine: "
            "_observe_director_phase_a boundary is unavailable"
        )

    started = time.monotonic()
    previous_marker = os.environ.get(_MARKER_ENV)
    os.environ[_MARKER_ENV] = "1"
    orchestrator._observe_director_phase_a = _planning_complete_stop
    try:
        try:
            production.main()
        except _PlanningBoundaryReached as reached:
            report = _validate_boundary(reached.output_dir)
            report["duration_seconds"] = round(time.monotonic() - started, 3)
            report["experiment_attempt"] = str(os.environ.get("ISCO_GEMINI_AB_ATTEMPT") or "")
            (reached.output_dir / REPORT_NAME).write_text(
                json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            print(
                "PRODUCTION_EXACT_PLANNING_HARNESS PASS: "
                f"output={reached.output_dir} media_started=false"
            )
            return reached.output_dir
        raise RuntimeError(
            "PRODUCTION_EXACT_PLANNING_HARNESS production entrypoint returned without "
            "crossing the post-planning boundary"
        )
    finally:
        orchestrator._observe_director_phase_a = original_boundary
        if previous_marker is None:
            os.environ.pop(_MARKER_ENV, None)
        else:
            os.environ[_MARKER_ENV] = previous_marker


def main() -> None:
    run_production_exact_planning_harness()


if __name__ == "__main__":
    main()

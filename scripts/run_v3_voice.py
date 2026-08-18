from __future__ import annotations

import json
import os
from pathlib import Path

import isco_video_agent.orchestrator as orchestrator
from scripts.planner_schema_guard import install_schema_guard
from scripts.product_proof_plan import install_product_proof_fallback, was_fallback_used
from scripts.task_level_planner_router import get_used_providers, install_router, write_planning_telemetry
from scripts.telegram_progress import install_progress_hooks, start_progress
from scripts.voice_mesh import install_voice_mesh

# Production-proof trigger only: no runtime behavior change.
# Run36 trigger only: no runtime behavior change.
# Run37 trigger only: no runtime behavior change.
# Run38 production trigger: صوت الآخرين في رأسك


def _resolve_plan_source() -> str:
    if was_fallback_used():
        return "product_proof_fallback"
    providers = get_used_providers()
    return "+".join(providers) if providers else "unknown"


def _tag_plan_source(out_dir: Path) -> None:
    """Record which planner actually produced this run's plan into every JSON
    artifact the workflow uploads (plan.json, quality-final.json), so a video's real
    planning source is never just an inference from log-scrollback."""
    source = _resolve_plan_source()
    for filename in ("plan.json", "quality-final.json"):
        path = out_dir / filename
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        data["plan_source"] = source
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Plan source tagged: {source}")


def _latest_output_dir() -> Path | None:
    roots = sorted(Path("output").glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
    return roots[0] if roots else None


def main() -> None:
    install_schema_guard()
    install_router()
    install_product_proof_fallback()
    install_voice_mesh()
    start_progress()
    install_progress_hooks()
    request = json.loads(Path(os.environ["REQUEST_FILE"]).read_text(encoding="utf-8"))
    try:
        out = orchestrator.produce(
            topic=request["topic"],
            requested_format=request["format"],
            dry_run=False,
            do_research=True,
        )
    except Exception:
        # planning-telemetry.json must exist on a failed run too - it's the exact
        # record needed to see which provider failed and why, without which this file
        # would only ever appear on the successful runs that need it least.
        out_dir = _latest_output_dir()
        if out_dir is not None:
            write_planning_telemetry(out_dir)
        raise
    _tag_plan_source(out)
    write_planning_telemetry(out)
    print(f"Production completed: {out.name}")


if __name__ == "__main__":
    main()

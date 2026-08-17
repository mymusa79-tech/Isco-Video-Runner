from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import isco_video_agent.orchestrator as orchestrator
from isco_video_agent.ai_budget import BudgetLedger
from scripts.planner_schema_guard import install_schema_guard
from scripts.product_proof_plan import install_product_proof_fallback
from scripts.run_v3_voice import _latest_output_dir, _tag_plan_source
from scripts.task_level_planner_router import install_router, write_planning_telemetry
from scripts.telegram_progress import install_progress_hooks, start_progress
from scripts.voice_mesh import install_voice_mesh


def _write_director_attempt_audit(ledger: BudgetLedger, out_dir: Path) -> None:
    attempts = []
    for attempt in getattr(ledger, "_attempts", []):
        if not str(attempt.task_id).startswith("DIRECTOR_"):
            continue
        attempts.append({
            "task_id": attempt.task_id,
            "provider": attempt.provider,
            "requested_model": attempt.requested_model,
            "resolved_model": attempt.resolved_model,
            "capability": attempt.capability.value,
            "outcome": attempt.outcome.value,
        })
    payload = {
        "director_provider_attempts": len(attempts),
        "hard_expected_max": 3,
        "within_cap": len(attempts) <= 3,
        "attempts": attempts,
    }
    (out_dir / "director-call-audit.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _write_final_hash(out_dir: Path) -> None:
    final = out_dir / "final.mp4"
    if not final.exists():
        return
    digest = hashlib.sha256(final.read_bytes()).hexdigest()
    (out_dir / "final.sha256").write_text(digest + "  final.mp4\n", encoding="utf-8")


def _write_diagnostics(ledger: BudgetLedger, out_dir: Path) -> None:
    write_planning_telemetry(out_dir)
    ledger.write(out_dir / "ai-budget.json")
    _write_director_attempt_audit(ledger, out_dir)
    _write_final_hash(out_dir)


def main() -> None:
    install_schema_guard()
    install_router()
    install_product_proof_fallback()
    install_voice_mesh()
    start_progress()
    install_progress_hooks()

    request = json.loads(Path(os.environ["REQUEST_FILE"]).read_text(encoding="utf-8"))
    ledger = BudgetLedger(request["format"])
    try:
        out = orchestrator.produce(
            topic=request["topic"],
            requested_format=request["format"],
            dry_run=False,
            do_research=True,
            ledger=ledger,
        )
    except Exception:
        out_dir = _latest_output_dir()
        if out_dir is not None:
            _write_diagnostics(ledger, out_dir)
        raise

    _tag_plan_source(out)
    _write_diagnostics(ledger, out)
    print(f"Director A+B one-off completed: {out.name}")


if __name__ == "__main__":
    main()

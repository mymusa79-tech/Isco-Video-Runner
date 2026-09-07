from __future__ import annotations

"""Execute Gold only over an exact SHA-bound QC_PENDING bundle.

No planner, research, stock retrieval, TTS, render, or Final Master QC entrypoint is
called here. The existing Final Master Acceptance is revalidated, durable history is
copied to a temporary file, the checkpointed core row is injected only into that copy,
and Gold remains the sole authority that may mark it accepted. Callers may persist the
accepted history copy only after this script exits successfully.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import isco_video_agent.orchestrator as orchestrator
from isco_video_agent.ai_budget import BudgetLedger
from isco_video_agent.config import secret
from isco_video_agent.production_pipeline import _output_key

from scripts.gold_enforce_phase4 import run_gold_enforce_phase4
from scripts.gold_vision_capacity_reserve_v1 import install_gold_vision_capacity_reserve_v1
from scripts.production_model_contract import install_production_model_contract
from scripts.qc_pending_resume_bundle_v1 import validate_resume_bundle
from scripts.runtime_closure import install_runtime_closure


CONTRACT_ID = "gold.qc-pending.resume-execution.v1"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"Gold resume requires JSON object: {path}")
    return value


def _git_head(root: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout.strip()


def _prepare_temporary_history(
    *,
    durable_history: Path,
    temporary_history: Path,
    pending_record: dict[str, Any],
    output_key: str,
) -> None:
    try:
        data = json.loads(Path(durable_history).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise RuntimeError("Gold resume could not read durable history") from exc
    if not isinstance(data, dict):
        raise RuntimeError("Gold resume durable history is not a JSON object")
    videos = data.setdefault("videos", [])
    if not isinstance(videos, list):
        raise RuntimeError("Gold resume durable history videos bucket is malformed")
    collisions = [
        item for item in videos
        if isinstance(item, dict) and str(item.get("output") or "").strip() == output_key
    ]
    if collisions:
        if any(str(item.get("release_status") or "") == "accepted_after_final_critic" for item in collisions):
            raise RuntimeError("Gold resume refused: output is already accepted in durable history")
        raise RuntimeError("Gold resume refused: output already exists in durable history")
    videos.append(json.loads(json.dumps(pending_record, ensure_ascii=False, default=str)))
    temporary_history.parent.mkdir(parents=True, exist_ok=True)
    temporary_history.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary_history.chmod(0o600)


def _verify_accepted_history(path: Path, *, output_key: str) -> dict[str, Any]:
    data = _read_object(path)
    videos = data.get("videos")
    if not isinstance(videos, list):
        raise RuntimeError("Gold resume accepted history videos bucket is malformed")
    matches = [
        item for item in videos
        if isinstance(item, dict) and str(item.get("output") or "").strip() == output_key
    ]
    if len(matches) != 1:
        raise RuntimeError("Gold resume did not preserve exactly one production history row")
    row = matches[0]
    if str(row.get("release_status") or "") != "accepted_after_final_critic":
        raise RuntimeError("Gold resume history row was not accepted by Gold")
    return row


def execute_gold_resume(
    *,
    bundle_dir: Path,
    durable_history: Path,
    accepted_history_output: Path,
    result_output: Path,
    expected_source_run_id: str,
    expected_runner_sha: str,
    expected_engine_sha: str,
) -> dict[str, Any]:
    root = Path(bundle_dir).resolve()
    manifest = validate_resume_bundle(
        root,
        expected_source_run_id=expected_source_run_id,
        expected_runner_sha=expected_runner_sha,
        expected_engine_sha=expected_engine_sha,
    )
    source = manifest["source"]
    checkpoint = _read_object(root / "qc-pending.json")
    state = checkpoint.get("production_state") or {}
    output_key = str(state.get("output_key") or "").strip()
    pending_record = state.get("record")
    if not isinstance(pending_record, dict) or not output_key:
        raise RuntimeError("Gold resume checkpoint state is incomplete")

    engine_root = Path(orchestrator.__file__).resolve().parents[2]
    runner_root = Path(__file__).resolve().parents[1]
    runner_head = _git_head(runner_root)
    engine_head = _git_head(engine_root)
    if runner_head != expected_runner_sha:
        raise RuntimeError("Gold resume runtime Runner checkout is not the certified source SHA")
    if engine_head != expected_engine_sha:
        raise RuntimeError("Gold resume runtime Engine checkout is not the checkpoint Engine SHA")
    if _output_key(root) != output_key:
        raise RuntimeError("Gold resume bundle is not restored at its original Engine output path")

    final_path = root / "final.mp4"
    final_sha_before = _sha256_file(final_path)
    if final_sha_before != manifest["final_sha256"]:
        raise RuntimeError("Gold resume final identity changed before execution")

    temporary_history = accepted_history_output.with_name(accepted_history_output.name + ".working")
    _prepare_temporary_history(
        durable_history=durable_history,
        temporary_history=temporary_history,
        pending_record=pending_record,
        output_key=output_key,
    )

    # Recreate the original production identity inside this Python process only. GitHub's
    # resume workflow has its own run id/SHA, but Gold provenance and any repeated
    # QC_PENDING checkpoint must remain bound to the source render and source code.
    os.environ["ISCO_HISTORY_PATH"] = str(temporary_history)
    os.environ["GITHUB_SHA"] = expected_runner_sha
    os.environ["GITHUB_REF"] = "refs/heads/main"
    os.environ["GITHUB_RUN_ID"] = str(source["run_id"])
    os.environ["GITHUB_RUN_ATTEMPT"] = str(source["run_attempt"])
    os.environ["ISCO_ENGINE_SHA"] = expected_engine_sha
    os.environ["ISCO_PRODUCTION_ID"] = f"v4:{source['run_id']}:{source['run_attempt']}"
    os.environ["ISCO_AI_BUDGET_ENFORCE"] = "1"

    gemini = secret("GEMINI_API_KEY")
    pexels = secret("PEXELS_API_KEY")
    pixabay = secret("PIXABAY_API_KEY")
    if not gemini or not pexels:
        raise RuntimeError("Gold resume requires Gemini and Pexels production credentials")

    install_production_model_contract(orchestrator)
    install_runtime_closure()
    install_gold_vision_capacity_reserve_v1()

    fmt = str(checkpoint.get("format") or "").strip().lower()
    if fmt not in {"film", "moment", "story"}:
        raise RuntimeError("Gold resume checkpoint format is invalid")
    ledger = BudgetLedger(fmt, enforce=True)

    plan, critic, gold_report = run_gold_enforce_phase4(
        output_dir=root,
        gemini=gemini,
        pexels=pexels,
        pixabay=pixabay,
        ledger=ledger,
    )
    final_sha_after = _sha256_file(final_path)
    if final_sha_after != final_sha_before:
        raise RuntimeError("Gold resume changed final.mp4")
    if gold_report.get("gold", {}).get("accepted") is not True:
        raise RuntimeError("Gold resume returned without Gold acceptance")
    if gold_report.get("viewer_quality", {}).get("verdict") != "pass":
        raise RuntimeError("Gold resume returned without Viewer Quality PASS")

    accepted_row = _verify_accepted_history(temporary_history, output_key=output_key)
    accepted_history_output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(temporary_history, accepted_history_output)
    accepted_history_output.chmod(0o600)
    ledger.write(root / "ai-budget-gold-resume.json")

    result = {
        "schema_version": 1,
        "contract_id": CONTRACT_ID,
        "status": "gold_accepted",
        "release_authority": "gold",
        "source_run_id": str(source["run_id"]),
        "source_runner_sha": expected_runner_sha,
        "source_engine_sha": expected_engine_sha,
        "format": str(getattr(plan, "format", fmt)),
        "final_sha256_before": final_sha_before,
        "final_sha256_after": final_sha_after,
        "final_media_mutated": False,
        "viewer_score_10": gold_report.get("viewer_quality", {}).get("score_10"),
        "production_history_release_status": accepted_row.get("release_status"),
        "planning_performed": False,
        "research_performed": False,
        "visual_retrieval_performed": False,
        "tts_performed": False,
        "rerender_performed": False,
        "final_master_qc_reexecuted": False,
        "durable_history_persisted_by_executor": False,
        "durable_history_ready_for_persistence": True,
        "critic_status": critic.get("status") if isinstance(critic, dict) else None,
    }
    result_output.parent.mkdir(parents=True, exist_ok=True)
    result_output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        temporary_history.unlink()
    except OSError:
        pass
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--durable-history", required=True, type=Path)
    parser.add_argument("--accepted-history-output", required=True, type=Path)
    parser.add_argument("--result", required=True, type=Path)
    parser.add_argument("--source-run-id", required=True)
    parser.add_argument("--runner-sha", required=True)
    parser.add_argument("--engine-sha", required=True)
    args = parser.parse_args()
    execute_gold_resume(
        bundle_dir=args.bundle,
        durable_history=args.durable_history,
        accepted_history_output=args.accepted_history_output,
        result_output=args.result,
        expected_source_run_id=args.source_run_id,
        expected_runner_sha=args.runner_sha,
        expected_engine_sha=args.engine_sha,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
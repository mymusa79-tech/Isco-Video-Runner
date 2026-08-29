from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import isco_video_agent.orchestrator as orchestrator
from isco_video_agent.ai_budget import BudgetLedger
from isco_video_agent.config import secret
from isco_video_agent.text_audit_router import text_audit_circuit_scope
from isco_video_agent.youtube_analytics import collect_latest_video_metrics_from_env
from scripts.analytics_observer_status import observe_post_acceptance_analytics
from scripts.append_retry_guard import install_append_retry_guard
from scripts.attempt9_schema_normalizer import install_attempt9_schema_normalizer
from scripts.brand_anchor_guard import install_brand_anchor_guard
from scripts.final_master_qc import run_final_master_qc
from scripts.gold_enforce_phase4 import run_gold_enforce_phase4
from scripts.m7_live_binding import install_m7_live_binding
from scripts.opening_feasibility_guard import install_opening_feasibility_guard
from scripts.planning_batch_hardening import install_planning_batch_hardening
from scripts.planner_quality_guard import install_planner_quality_guard
from scripts.planner_schema_guard import install_schema_guard
from scripts.product_proof_plan import install_product_proof_fallback, was_fallback_used
from scripts.production_model_contract import install_production_model_contract
from scripts.provider_capacity_hardening import install_provider_capacity_hardening
from scripts.run120_dossier_repair_hardening import install_run120_dossier_repair_hardening
from scripts.run120_schema_policy_bridge import install_run120_schema_policy_bridge
from scripts.run123_budget_closure import install_run123_budget_closure
from scripts.runtime_closure import install_runtime_closure, run_post_gold_observers
from scripts.schema_repair_policy import install_schema_repair_policy
from scripts.task_level_planner_router import get_used_providers, install_router, write_planning_telemetry
from scripts.telegram_progress import install_progress_hooks, start_progress
from scripts.voice_identity_observer import install_voice_identity_observer
from scripts.voice_mesh import install_voice_mesh

# Production-proof trigger only: no runtime behavior change.
# Run36 trigger only: no runtime behavior change.
# Run37 trigger only: no runtime behavior change.
# Run38 production trigger: صوت الآخرين في رأسك
# Final production trigger after append-only length enforcement: صوت الآخرين في رأسك
# Decisive production trigger after full-suite validation: صوت الآخرين في رأسك


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


def _attach_failure_tone_diagnostics(out_dir: Path) -> None:
    """Copy the full tone audit into quality-precheck.json on failed production.

    The Engine already writes tone-quality-audit.json before enforcing the tone gate,
    but V4's failure artifact uploads quality-precheck.json and not that sidecar. Keep
    this observational only: enrich the already-uploaded precheck when both files are
    valid JSON objects, and never mask the original production exception.
    """
    precheck_path = out_dir / "quality-precheck.json"
    tone_path = out_dir / "tone-quality-audit.json"
    if not precheck_path.is_file() or not tone_path.is_file():
        return
    try:
        precheck = json.loads(precheck_path.read_text(encoding="utf-8"))
        tone = json.loads(tone_path.read_text(encoding="utf-8"))
        if not isinstance(precheck, dict) or not isinstance(tone, dict):
            return
        precheck["tone_quality_audit"] = tone
        precheck_path.write_text(json.dumps(precheck, ensure_ascii=False, indent=2), encoding="utf-8")
        print("Tone failure diagnostics attached to quality-precheck.json")
    except Exception as exc:
        print(f"Tone failure diagnostics attachment skipped ({type(exc).__name__})")


def _attach_voice_audit_to_telemetry(telemetry_path: Path, output_dir: Path) -> None:
    """Embed Voice Observer evidence in durable telemetry without making it a gate."""
    voice_path = output_dir / "voice-identity-audit.json"
    if not voice_path.is_file() or not telemetry_path.is_file():
        return
    try:
        telemetry = json.loads(telemetry_path.read_text(encoding="utf-8"))
        voice = json.loads(voice_path.read_text(encoding="utf-8"))
        if not isinstance(telemetry, dict) or not isinstance(voice, dict):
            return
        telemetry["voice_identity_audit"] = voice
        telemetry_path.write_text(json.dumps(telemetry, ensure_ascii=False, indent=2), encoding="utf-8")
        print("Voice Identity Observer evidence attached to planning telemetry")
    except Exception as exc:
        print(f"Voice Identity Observer telemetry attachment skipped ({type(exc).__name__})")


def _production_id() -> str:
    run_id = (os.environ.get("GITHUB_RUN_ID") or "local").strip()
    attempt = (os.environ.get("GITHUB_RUN_ATTEMPT") or "1").strip()
    return f"v4:{run_id}:{attempt}"


def _production_budget_ledger(fmt: str) -> BudgetLedger:
    """Production is the enforcing owner of the AI attempt budget.

    Tests and offline diagnostics may keep BudgetLedger's observe-only default, but a
    real V4 run must never silently exceed task-local or run-wide provider ceilings.
    """
    os.environ["ISCO_AI_BUDGET_ENFORCE"] = "1"
    return BudgetLedger(fmt)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_production_manifest(out: Path, *, production_id: str, fmt: str) -> dict:
    final_path = out / "final.mp4"
    if not final_path.is_file():
        raise RuntimeError("Final video missing before production manifest")
    video_id = (os.environ.get("ISCO_PRODUCTION_VIDEO_ID") or "").strip()
    binding_source = (os.environ.get("ISCO_PRODUCTION_BINDING_SOURCE") or "").strip()
    run_number = (os.environ.get("GITHUB_RUN_NUMBER") or "").strip()
    verified = bool(video_id and binding_source)
    manifest = {
        "schema_version": 1,
        "production_id": production_id,
        "github_run_id": (os.environ.get("GITHUB_RUN_ID") or "").strip() or None,
        "github_run_number": run_number or None,
        "github_run_attempt": (os.environ.get("GITHUB_RUN_ATTEMPT") or "").strip() or None,
        "runner_sha": (os.environ.get("GITHUB_SHA") or "").strip() or None,
        "engine_sha": (os.environ.get("ISCO_ENGINE_SHA") or "").strip() or None,
        "release_tag": f"video-{run_number}" if run_number else None,
        "format": fmt,
        "final_sha256": _sha256_file(final_path),
        "release_authority": "gold_enforced",
        "youtube_video_id": video_id or None,
        "publication_binding": "verified" if verified else "unbound",
        "binding_source": binding_source or None,
    }
    (out / "production-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def _attach_observer_evidence_to_telemetry(
    telemetry_path: Path,
    *,
    manifest: dict,
    critic: dict,
    ledger: BudgetLedger,
    output_dir: Path,
    gold_enforce: dict | None = None,
    analytics_status: dict | None = None,
) -> None:
    """Make existing release telemetry the durable provenance/Gold envelope."""
    data = json.loads(telemetry_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("planning telemetry must be a JSON object")
    data["production_manifest"] = manifest
    data["final_critic"] = critic
    data["ai_budget"] = ledger.to_summary()
    if isinstance(gold_enforce, dict):
        data["gold_enforce_report"] = gold_enforce
    if isinstance(analytics_status, dict):
        data["analytics_observer_status"] = analytics_status
    opening_path = output_dir / "opening-visual-audit.json"
    if opening_path.exists():
        opening = json.loads(opening_path.read_text(encoding="utf-8"))
        if isinstance(opening, dict):
            data["opening_visual_audit"] = opening
    voice_path = output_dir / "voice-identity-audit.json"
    if voice_path.exists():
        voice = json.loads(voice_path.read_text(encoding="utf-8"))
        if isinstance(voice, dict):
            data["voice_identity_audit"] = voice
    master_qc_path = output_dir / "final-master-qc.json"
    if master_qc_path.exists():
        master_qc = json.loads(master_qc_path.read_text(encoding="utf-8"))
        if isinstance(master_qc, dict):
            data["final_master_qc"] = master_qc
    telemetry_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    # Runner is the sole V4 production authority for the concrete provider models.
    # Install and prove that contract before any capacity/retry policy or provider work.
    install_production_model_contract(orchestrator)
    # Install the recalculated run-wide envelope before any production BudgetLedger is
    # constructed. This closes the stale 42/34 policy from the pre-Gold production graph.
    install_run123_budget_closure()
    install_schema_guard()
    # Capacity policy is installed before provider routing so every planning subtask
    # inherits token-aware admission, bounded completion reserves and Retry-After.
    install_provider_capacity_hardening()
    install_router()
    # Keep one global canonical outline, but split only output-heavy writer/doctor
    # calls. The existing quality guard wraps this batch writer afterwards, preserving
    # the single-use transition and all downstream hard quality gates.
    install_planning_batch_hardening()
    # Reuse the Runner's existing bounded schema-recovery owner in real production.
    # It retries only local shape/id/order defects; provider/router/auth/network/budget
    # failures are not replayed. Partial Script Doctor output completes only missing ids.
    install_schema_repair_policy()
    # Run #120: preserve the successful base plan during RepairDossier. Install before
    # planner_quality_guard so the existing tone wrapper remains outside both initial
    # planning and in-place dossier repair. The bridge dynamically calls the bounded
    # schema owner above; only pure length/capacity pressure may shrink 2 -> 1.
    install_run120_dossier_repair_hardening()
    install_run120_schema_policy_bridge()
    install_planner_quality_guard()
    install_attempt9_schema_normalizer()
    install_append_retry_guard()
    install_runtime_closure()
    install_brand_anchor_guard()
    install_product_proof_fallback()
    install_voice_mesh()
    install_voice_identity_observer()
    install_m7_live_binding()
    # Run #93: install_m7_live_binding() installs Security V1 (its length-validating
    # stock-search wrapper) as a side effect. The opening feasibility guard must wrap
    # AROUND that validator - not be wrapped BY it - so its query-shortening logic runs
    # before Security V1 sees the query, not after. Explicit, ordered installation here
    # (matching every other guard in this sequence) guarantees that regardless of
    # module import order; the previous implicit install-on-package-import in
    # scripts/__init__.py raced Security V1's later install and sometimes lost.
    install_opening_feasibility_guard()
    start_progress()
    install_progress_hooks()

    request = json.loads(Path(os.environ["REQUEST_FILE"]).read_text(encoding="utf-8"))
    gemini = secret("GEMINI_API_KEY")
    pexels = secret("PEXELS_API_KEY")
    pixabay = secret("PIXABAY_API_KEY")
    if not gemini or not pexels:
        raise RuntimeError("Gemini and Pexels secrets are required for V4 production")
    # Runner remains the one-time owner of provider inputs needed both by the core and
    # by post-render Gold packaging. Restored env copies are consumed once by the core;
    # Gold reuses only these in-process values.
    os.environ["GEMINI_API_KEY"] = gemini
    os.environ["PEXELS_API_KEY"] = pexels
    if pixabay:
        os.environ["PIXABAY_API_KEY"] = pixabay
    ledger = _production_budget_ledger(request["format"])
    production_id = _production_id()
    os.environ["ISCO_PRODUCTION_ID"] = production_id

    previous_defer = os.environ.get("ISCO_DEFER_YOUTUBE_ANALYTICS")
    os.environ["ISCO_DEFER_YOUTUBE_ANALYTICS"] = "1"
    try:
        # Share text-audit provider health only across the core audits/re-audits of
        # this one production run. The Engine context resets even on failure, so a
        # later run never inherits a stale circuit. Gold Final Critic has its own
        # separately bounded provider policy and remains outside this scope.
        with text_audit_circuit_scope():
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
            _attach_failure_tone_diagnostics(out_dir)
            ledger.write(out_dir / "ai-budget.json")
            telemetry_path = write_planning_telemetry(out_dir)
            _attach_voice_audit_to_telemetry(telemetry_path, out_dir)
        raise
    finally:
        if previous_defer is None:
            os.environ.pop("ISCO_DEFER_YOUTUBE_ANALYTICS", None)
        else:
            os.environ["ISCO_DEFER_YOUTUBE_ANALYTICS"] = previous_defer

    _tag_plan_source(out)
    try:
        # Final Master QC is the last media-integrity gate on the exact rendered file.
        # It runs before Gold can accept/mutate state and never changes final.mp4.
        run_final_master_qc(out)
        plan, critic, gold_enforce = run_gold_enforce_phase4(
            output_dir=out,
            gemini=gemini,
            pexels=pexels,
            pixabay=pixabay,
            ledger=ledger,
        )
    except Exception:
        # Preserve the enforcing failure as the workflow result, but flush the exact
        # same-ledger evidence and Voice Observer diagnostics first. No manifest,
        # analytics, release, or accepted publication evidence is written on failure.
        ledger.write(out / "ai-budget.json")
        telemetry_path = write_planning_telemetry(out)
        _attach_voice_audit_to_telemetry(telemetry_path, out)
        raise

    run_post_gold_observers(out)
    ledger.write(out / "ai-budget.json")
    manifest = _write_production_manifest(out, production_id=production_id, fmt=plan.format)

    # Analytics remains strictly post-acceptance and non-authoritative. Gold has
    # already passed and the production manifest exists before this observer runs.
    # A collector failure is durable evidence, never a release veto and never silence.
    analytics_status = observe_post_acceptance_analytics(
        out,
        collector=collect_latest_video_metrics_from_env,
        format_hint=plan.format,
        expected_video_id=manifest.get("youtube_video_id"),
        production_id=production_id if manifest.get("publication_binding") == "verified" else None,
        binding_source=manifest.get("binding_source"),
    )

    telemetry_path = write_planning_telemetry(out)
    _attach_observer_evidence_to_telemetry(
        telemetry_path,
        manifest=manifest,
        critic=critic,
        ledger=ledger,
        output_dir=out,
        gold_enforce=gold_enforce,
        analytics_status=analytics_status,
    )
    print(f"Production completed: {out.name}")


if __name__ == "__main__":
    main()

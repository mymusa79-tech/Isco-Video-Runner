from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import isco_video_agent.orchestrator as orchestrator
from isco_video_agent.ai_budget import BudgetLedger
from isco_video_agent.config import secret
from isco_video_agent.production_pipeline import _plan_from_json, _run_final_critic
from isco_video_agent.youtube_analytics import collect_latest_video_metrics_from_env
from scripts.append_retry_guard import install_append_retry_guard
from scripts.brand_anchor_guard import install_brand_anchor_guard
from scripts.gold_shadow_phase2b import run_gold_shadow_phase2b
from scripts.planner_quality_guard import install_planner_quality_guard
from scripts.planner_schema_guard import install_schema_guard
from scripts.product_proof_plan import install_product_proof_fallback, was_fallback_used
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
    gold_shadow: dict | None = None,
) -> None:
    """Make existing release telemetry the durable provenance/observer envelope."""
    data = json.loads(telemetry_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("planning telemetry must be a JSON object")
    data["production_manifest"] = manifest
    data["final_critic"] = critic
    data["ai_budget"] = ledger.to_summary()
    if isinstance(gold_shadow, dict):
        data["gold_shadow_comparison"] = gold_shadow
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
    telemetry_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    install_schema_guard()
    install_router()
    install_planner_quality_guard()
    install_append_retry_guard()
    install_brand_anchor_guard()
    install_product_proof_fallback()
    install_voice_mesh()
    install_voice_identity_observer()
    start_progress()
    install_progress_hooks()

    request = json.loads(Path(os.environ["REQUEST_FILE"]).read_text(encoding="utf-8"))
    gemini = secret("GEMINI_API_KEY")
    pexels = secret("PEXELS_API_KEY")
    if not gemini or not pexels:
        raise RuntimeError("Gemini and Pexels secrets are required for V4 production")
    # Runner is now the one-time owner of both post-render observer secrets. The core
    # receives in-process env copies, consumes/pops them, and no secret file is read a
    # second time after rendering.
    os.environ["GEMINI_API_KEY"] = gemini
    os.environ["PEXELS_API_KEY"] = pexels
    ledger = BudgetLedger(request["format"])

    previous_defer = os.environ.get("ISCO_DEFER_YOUTUBE_ANALYTICS")
    os.environ["ISCO_DEFER_YOUTUBE_ANALYTICS"] = "1"
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
    plan = _plan_from_json(out / "plan.json")
    content_model = os.environ.get("GEMINI_CONTENT_MODEL", "gemini-2.5-flash") or "gemini-2.5-flash"
    critic = _run_final_critic(
        output_dir=out,
        plan=plan,
        gemini=gemini,
        content_model=content_model,
        ledger=ledger,
        release_mode="observe_only",
    )
    if critic.get("status") != "pass":
        print("Final Critic observe-only BLOCK: publication path unchanged; inspect final-critic.json")

    gold_shadow = run_gold_shadow_phase2b(
        output_dir=out,
        gemini=gemini,
        pexels=pexels,
        ledger=ledger,
        legacy_critic=critic,
        plan_from_json=_plan_from_json,
        run_final_critic=_run_final_critic,
    )

    ledger.write(out / "ai-budget.json")
    production_id = _production_id()
    manifest = _write_production_manifest(out, production_id=production_id, fmt=plan.format)

    # The observer may classify agent_produced only when this run carries an explicit
    # verified video binding. Otherwise the latest channel video is kept as unverified
    # observational data and cannot enter agent cohorts or block historical backfill.
    try:
        collect_latest_video_metrics_from_env(
            format_hint=plan.format,
            expected_video_id=manifest.get("youtube_video_id"),
            production_id=production_id if manifest.get("publication_binding") == "verified" else None,
            binding_source=manifest.get("binding_source"),
        )
    except Exception:
        pass

    telemetry_path = write_planning_telemetry(out)
    _attach_observer_evidence_to_telemetry(
        telemetry_path,
        manifest=manifest,
        critic=critic,
        ledger=ledger,
        output_dir=out,
        gold_shadow=gold_shadow,
    )
    print(f"Production completed: {out.name}")


if __name__ == "__main__":
    main()

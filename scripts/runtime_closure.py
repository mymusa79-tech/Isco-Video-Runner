from __future__ import annotations

import os
from pathlib import Path

import isco_video_agent.orchestrator as orchestrator
from scripts.audio_mastering_live_binding import install_audio_mastering_live_binding
from scripts.audio_semantic_integrity import (
    install_audio_semantic_final_gate,
    install_audio_semantic_integrity_binding,
)
from scripts.final_qc_observer_cache_trust import sanitize_final_observer_cache_before_runtime
from scripts.final_qc_observer_durability import (
    install_final_qc_observer_durability,
    run_groq_audio_audit_durable,
)
from scripts.groq_audio_audit import DEFAULT_AUDIO_MODEL, run_groq_audio_audit
from scripts.narrative_music_dynamics import install_narrative_music_dynamics
from scripts.orchestration_cinematic_port import (
    CinematicInstallPhase,
    install_cinematic_runtime_port,
)
from scripts.orchestration_media_port import install_media_runtime_port
from scripts.orchestration_render_port import install_render_runtime_port
from scripts.planning_checkpoint_state import install_runtime_persistence_wrapper
from scripts.planning_runtime_contract import install_runtime_planning_contracts
from scripts.producer_quality_contract import install_producer_handoff_contract
from scripts.runtime_phase import canonical_runtime_enabled
from scripts.runtime_reliability import (
    install_core_reliability_guard,
    install_release_transaction_guard,
    install_telemetry_reliability_binding,
    manifest_wrapper_chain_has_marker,
    production_entrypoint_modules,
)
from scripts.text_audit_provider_mesh import install_text_audit_provider_mesh


_TRUE_VALUES = {"1", "true", "yes", "on"}


def _groq_key() -> str:
    direct = (os.environ.get("GROQ_API_KEY") or "").strip()
    if direct:
        return direct
    file_name = (os.environ.get("GROQ_API_KEY_FILE") or "").strip()
    if not file_name:
        return ""
    try:
        return Path(file_name).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return ""


def _canonical_v4_bundle_enabled() -> bool:
    explicit = str(os.environ.get("ISCO_CANONICAL_V4_BUNDLE_ENABLED") or "").strip().lower()
    if explicit in _TRUE_VALUES:
        return True
    # Run130 closure: bundle activation shares the same application-owned phase source
    # as durable runtime state. Merely executing a test inside Production V4 is not
    # enough to make it a live production process.
    return canonical_runtime_enabled()


def install_canonical_v4_bundle_post_manifest() -> None:
    """Bind unified long+Short delivery in package imports and the real script entrypoint."""
    for production in production_entrypoint_modules():
        current = getattr(production, "_write_production_manifest")
        if manifest_wrapper_chain_has_marker(current, "_isco_canonical_v4_bundle"):
            continue

        def make_wrapper(original):
            def wrapped(out: Path, *, production_id: str, fmt: str):
                manifest = original(out, production_id=production_id, fmt=fmt)
                control_request = str(os.environ.get("ISCO_CONTROL_REQUEST_ID") or "").strip()
                if fmt != "moment" and _canonical_v4_bundle_enabled() and not control_request:
                    from scripts.canonical_v4_bundle import build_canonical_v4_bundle

                    delivery = build_canonical_v4_bundle(Path(out))
                    if delivery is None or not Path(delivery).is_file():
                        raise RuntimeError(
                            "Canonical V4 long-form production finished without unified delivery manifest"
                        )
                return manifest
            return wrapped

        wrapped = make_wrapper(current)
        wrapped._isco_canonical_v4_bundle = True
        wrapped._isco_canonical_v4_original = current
        setattr(production, "_write_production_manifest", wrapped)


def install_runtime_closure() -> None:
    """Install bounded production recovery plus cinematic and delivery stages."""
    runtime_active = canonical_runtime_enabled()

    # Retry/recovery ownership first; core preflight is evaluated lazily at produce().
    # Planning-affecting composition now has one canonical seam, including immutable
    # approved-input rebinding. This call preserves the exact historical V4 ordering
    # while keeping media/audio/release code outside the durable planning contract hash.
    install_runtime_planning_contracts()

    # Text audits are post-planning mandatory gates. Install their provider-portable
    # mesh only after planning capacity state is live, so Groq reuses the same model-
    # scoped headroom/reset evidence without entering the planning checkpoint contract.
    install_text_audit_provider_mesh()

    # L7.2 moves only the Media installation topology behind one stable seam. The port
    # preserves the certified historical order: Provider Capacity V2 -> Media Trust V2
    # -> durable asset/decision cache -> M8-composed prepared cache -> Pexels search
    # cache. Provider selection, retry ownership, trust/security decisions, cache
    # semantics, hit revalidation, and write policy remain in their existing owners.
    # L7.3 moves only the inner Cinematic composition behind one phase-explicit stable
    # seam. INNER preserves the exact historical SFX -> M8 -> M9 -> M10 -> CTA order.
    # The OUTER M7/M11 phase remains intentionally later in run_v3_voice after TTS, so
    # wrapper nesting and the opening-feasibility guard boundary do not change.
    # L7.4 moves only Render installation behind one stable seam. Durable cache
    # semantics, fingerprints, promotion/eviction, and current Engine QC revalidation
    # remain byte-for-byte owned by render_durable_cache.py.
    # Render durability is deliberately installed only after those inner cinematic
    # wrappers have been registered, but immediately before Narrative Music Dynamics
    # wraps the global mux. It patches only M9's expensive xfade-pair renderer,
    # orchestrator.burn_srt, and the underlying Engine mux. Consequently every SFX/M10/
    # CTA/Narrative wrapper still executes on a durable final hit and writes fresh reports.
    # Audio Semantic Integrity was registered earlier and remains the outer runtime mux
    # authority; current Engine QC probes every restored final before it stays durable.
    # Canonical bundle is bound on every live run_v3_voice module before the release
    # transaction wrapper, so `delivery_complete` means manifest + sibling Shorts both
    # returned in the actual `python ../scripts/run_v3_voice.py` process, not only tests.
    install_media_runtime_port()
    install_core_reliability_guard()
    install_audio_semantic_integrity_binding()
    install_audio_mastering_live_binding()
    install_cinematic_runtime_port(CinematicInstallPhase.INNER)
    install_render_runtime_port()
    install_narrative_music_dynamics()
    install_canonical_v4_bundle_post_manifest()
    install_release_transaction_guard()
    install_telemetry_reliability_binding()
    install_audio_semantic_final_gate(production_entrypoint_modules())
    # Producer handoff is installed after the independent Audio Semantic wrapper so it
    # becomes the outermost pre-Final-QC acceptance step:
    # Producer stage evidence -> Audio Semantic Integrity -> Final Master QC -> Gold.
    # It makes no AI calls and does not replace or weaken any downstream gate.
    install_producer_handoff_contract(production_entrypoint_modules())
    # Final QC/Observer durability is optimization-only and is installed after all
    # media/release authorities are already composed. Its restored cache parent is
    # sanitized first so a symlinked Actions-cache namespace becomes a clean miss.
    # It then patches only the imported run_v3_voice Final Master QC call and Voice
    # Identity observe_output boundary. Groq uses an explicit durable wrapper below;
    # analytics remains intentionally live.
    sanitize_final_observer_cache_before_runtime()
    install_final_qc_observer_durability()
    # Durable resume is deliberately outermost: every existing production/quality/safety
    # wrapper remains untouched and authoritative. This layer only persists the local
    # planner checkpoint after a failure, or writes a completion marker after success.
    if runtime_active:
        install_runtime_persistence_wrapper(orchestrator)


def _run_groq_audio_audit_compat(output_dir: Path, *, api_key: str | None, model: str = DEFAULT_AUDIO_MODEL) -> dict:
    """Preserve the historical default-model call shape for tests and diagnostics."""
    if model == DEFAULT_AUDIO_MODEL:
        return run_groq_audio_audit(Path(output_dir), api_key=api_key)
    return run_groq_audio_audit(Path(output_dir), api_key=api_key, model=model)


def run_post_gold_observers(output_dir: Path) -> dict:
    """Run G1/G2 only after Gold has accepted the final render."""
    try:
        return run_groq_audio_audit_durable(
            Path(output_dir),
            api_key=_groq_key(),
            original=_run_groq_audio_audit_compat,
        )
    except Exception as exc:
        print(
            f"Runtime post-Gold observer skipped ({type(exc).__name__}); production unchanged"
        )
        return {
            "schema_version": 1,
            "mode": "observe_only",
            "decision": "audit_error",
            "audit_error": f"{type(exc).__name__}: {str(exc)[:240]}",
        }

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

    install_runtime_planning_contracts()
    install_text_audit_provider_mesh()

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

    # Optimization must be INNER to every mandatory final acceptance authority. A
    # durable Final-QC PASS may skip the expensive deterministic QC implementation,
    # but it must never skip current-run producer evidence or Audio Semantic Integrity.
    sanitize_final_observer_cache_before_runtime()
    install_final_qc_observer_durability()
    install_audio_semantic_final_gate(production_entrypoint_modules())
    install_producer_handoff_contract(production_entrypoint_modules())
    # Effective call order is now:
    # Producer Handoff -> Audio Semantic Integrity -> Durable Final QC -> Final QC.
    # Gold remains outside this call and is reached only after the entire chain returns.

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

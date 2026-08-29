from __future__ import annotations

import os
from pathlib import Path

import isco_video_agent.orchestrator as orchestrator
from scripts.audio_mastering_live_binding import install_audio_mastering_live_binding
from scripts.audio_semantic_integrity import (
    install_audio_semantic_final_gate,
    install_audio_semantic_integrity_binding,
)
from scripts.cta_live_binding import install_cta_live_binding
from scripts.final_qc_observer_durability import (
    install_final_qc_observer_durability,
    run_groq_audio_audit_durable,
)
from scripts.groq_audio_audit import run_groq_audio_audit
from scripts.m8_live_binding import install_m8_live_binding
from scripts.m9_live_binding import install_m9_live_binding
from scripts.m10_live_binding import install_m10_live_binding
from scripts.media_durable_cache import install_media_durable_cache
from scripts.media_prepared_live_cache import install_media_prepared_live_cache
from scripts.media_search_durable_cache import install_media_search_durable_cache
from scripts.media_trust_boundary_v2 import install_media_trust_boundary_v2
from scripts.narrative_music_dynamics import install_narrative_music_dynamics
from scripts.planning_checkpoint_state import install_runtime_persistence_wrapper
from scripts.planning_runtime_contract import install_runtime_planning_contracts
from scripts.provider_capacity_v2 import install_provider_capacity_v2
from scripts.render_durable_cache import install_render_durable_cache
from scripts.runtime_phase import canonical_runtime_enabled
from scripts.runtime_reliability import (
    install_core_reliability_guard,
    install_release_transaction_guard,
    install_telemetry_reliability_binding,
    manifest_wrapper_chain_has_marker,
    production_entrypoint_modules,
)
from scripts.sfx_live_binding import install_sfx_live_binding


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

    # Pixabay Provider Capacity V2 owns its provider-required 24h metadata cache. Media
    # Trust then establishes exact-byte/security authority. Media durability may reuse
    # only decisions/derivatives bound to those bytes, and every durable Vision hit
    # re-runs current local trust/security. The live prepared bridge is installed before
    # M8's produce wrapper so its inner scope executes only after M8 has replaced the
    # prepare seam; this keeps the durable wrapper outside the real M8 renderer instead
    # of leaving a tested-but-bypassed static wrapper. Pexels metadata remains last in
    # the media group so both providers can resume search work without changing provider
    # order, AI budgets, or retry ownership.
    # Render durability is deliberately installed only after the live M9/M10/CTA produce
    # wrappers have been registered, but immediately before Narrative Music Dynamics
    # wraps the global mux. It patches only M9's expensive xfade-pair renderer,
    # orchestrator.burn_srt, and the underlying Engine mux. Consequently every SFX/M10/
    # CTA/Narrative wrapper still executes on a durable final hit and writes fresh reports.
    # Audio Semantic Integrity was registered earlier and remains the outer runtime mux
    # authority; current Engine QC probes every restored final before it stays durable.
    # Canonical bundle is bound on every live run_v3_voice module before the release
    # transaction wrapper, so `delivery_complete` means manifest + sibling Shorts both
    # returned in the actual `python ../scripts/run_v3_voice.py` process, not only tests.
    install_provider_capacity_v2()
    install_media_trust_boundary_v2()
    install_media_durable_cache()
    install_media_prepared_live_cache()
    install_media_search_durable_cache()
    install_core_reliability_guard()
    install_audio_semantic_integrity_binding()
    install_audio_mastering_live_binding()
    install_sfx_live_binding()
    install_m8_live_binding()
    install_m9_live_binding()
    install_m10_live_binding()
    install_cta_live_binding()
    install_render_durable_cache()
    install_narrative_music_dynamics()
    install_canonical_v4_bundle_post_manifest()
    install_release_transaction_guard()
    install_telemetry_reliability_binding()
    install_audio_semantic_final_gate(production_entrypoint_modules())
    # Final QC/Observer durability is optimization-only and is installed after all
    # media/release authorities are already composed. It patches only the imported
    # run_v3_voice Final Master QC call and Voice Identity observe_output boundary.
    # Groq uses an explicit durable wrapper below; analytics remains intentionally live.
    install_final_qc_observer_durability()
    # Durable resume is deliberately outermost: every existing production/quality/safety
    # wrapper remains untouched and authoritative. This layer only persists the local
    # planner checkpoint after a failure, or writes a completion marker after success.
    if runtime_active:
        install_runtime_persistence_wrapper(orchestrator)


def run_post_gold_observers(output_dir: Path) -> dict:
    """Run G1/G2 only after Gold has accepted the final render."""
    try:
        return run_groq_audio_audit_durable(
            Path(output_dir),
            api_key=_groq_key(),
            original=run_groq_audio_audit,
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

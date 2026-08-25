from __future__ import annotations

import os
from pathlib import Path

from scripts.attempt10_append_bound_recovery import install_attempt10_append_bound_recovery
from scripts.audio_mastering_live_binding import install_audio_mastering_live_binding
from scripts.bounded_output_recovery import install_bounded_output_recovery
from scripts.cta_live_binding import install_cta_live_binding
from scripts.gemini_planning_output_guard import install_gemini_planning_output_guard
from scripts.groq_audio_audit import run_groq_audio_audit
from scripts.m8_live_binding import install_m8_live_binding
from scripts.m9_live_binding import install_m9_live_binding
from scripts.m10_live_binding import install_m10_live_binding
from scripts.media_trust_boundary_v2 import install_media_trust_boundary_v2
from scripts.narrative_music_dynamics import install_narrative_music_dynamics
from scripts.provider_capacity_v2 import install_provider_capacity_v2
from scripts.runtime_reliability import (
    install_core_reliability_guard,
    install_release_transaction_guard,
    install_telemetry_reliability_binding,
    manifest_wrapper_chain_has_marker,
    production_entrypoint_modules,
)
from scripts.schema_repair_policy import install_schema_repair_policy
from scripts.sfx_live_binding import install_sfx_live_binding


_CANONICAL_V4_WORKFLOW = "/.github/workflows/produce-resilient-v4.yml@"
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
    event = str(os.environ.get("GITHUB_EVENT_NAME") or "").strip()
    workflow_ref = str(os.environ.get("GITHUB_WORKFLOW_REF") or "").strip()
    return event == "workflow_dispatch" and _CANONICAL_V4_WORKFLOW in workflow_ref


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
    # Retry/recovery ownership first; core preflight is evaluated lazily at produce().
    # Media Trust and Provider Capacity do not change AI-call or retry ownership.
    # They bind exact stock bytes and the documented Pixabay 24h search cache before
    # the reliability contract is frozen for produce().
    # Canonical bundle is bound on every live run_v3_voice module before the release
    # transaction wrapper, so `delivery_complete` means manifest + sibling Shorts both
    # returned in the actual `python ../scripts/run_v3_voice.py` process, not only tests.
    install_attempt10_append_bound_recovery()
    install_bounded_output_recovery()
    install_schema_repair_policy()
    install_gemini_planning_output_guard()
    install_media_trust_boundary_v2()
    install_provider_capacity_v2()
    install_core_reliability_guard()
    install_audio_mastering_live_binding()
    install_sfx_live_binding()
    install_m8_live_binding()
    install_m9_live_binding()
    install_m10_live_binding()
    install_cta_live_binding()
    install_narrative_music_dynamics()
    install_canonical_v4_bundle_post_manifest()
    install_release_transaction_guard()
    install_telemetry_reliability_binding()


def run_post_gold_observers(output_dir: Path) -> dict:
    """Run G1/G2 only after Gold has accepted the final render."""
    try:
        return run_groq_audio_audit(
            Path(output_dir),
            api_key=_groq_key(),
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

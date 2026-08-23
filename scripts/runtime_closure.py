from __future__ import annotations

import os
from pathlib import Path

from scripts.attempt10_append_bound_recovery import install_attempt10_append_bound_recovery
from scripts.audio_mastering_live_binding import install_audio_mastering_live_binding
from scripts.cta_live_binding import install_cta_live_binding
from scripts.groq_audio_audit import run_groq_audio_audit
from scripts.m8_live_binding import install_m8_live_binding
from scripts.m9_live_binding import install_m9_live_binding
from scripts.m10_live_binding import install_m10_live_binding
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
    """Return true only inside the canonical V4 workflow or an explicit test harness.

    The delivery layer must never redefine the generic production-manifest helper.
    GitHub production is identified by the exact workflow path, not by a mutable
    display name. Local/focused contract tests may opt in explicitly.
    """
    explicit = str(os.environ.get("ISCO_CANONICAL_V4_BUNDLE_ENABLED") or "").strip().lower()
    if explicit in _TRUE_VALUES:
        return True
    event = str(os.environ.get("GITHUB_EVENT_NAME") or "").strip()
    workflow_ref = str(os.environ.get("GITHUB_WORKFLOW_REF") or "").strip()
    return event == "workflow_dispatch" and _CANONICAL_V4_WORKFLOW in workflow_ref


def install_canonical_v4_bundle_post_manifest() -> None:
    """Make canonical V4 long-form delivery atomic with 2–3 sibling Shorts.

    The hook is inert for generic library/tests, Moments, and control-plane runs.
    It activates only for canonical V4 after the long render has passed Gold and its
    production manifest has been written.
    """
    import scripts.run_v3_voice as production

    current = production._write_production_manifest
    if getattr(current, "_isco_canonical_v4_bundle", False):
        return

    def wrapped(out: Path, *, production_id: str, fmt: str):
        manifest = current(out, production_id=production_id, fmt=fmt)
        control_request = str(os.environ.get("ISCO_CONTROL_REQUEST_ID") or "").strip()
        if fmt != "moment" and _canonical_v4_bundle_enabled() and not control_request:
            from scripts.canonical_v4_bundle import build_canonical_v4_bundle

            delivery = build_canonical_v4_bundle(Path(out))
            if delivery is None or not Path(delivery).is_file():
                raise RuntimeError("Canonical V4 long-form production finished without unified delivery manifest")
        return manifest

    wrapped._isco_canonical_v4_bundle = True
    wrapped._isco_canonical_v4_original = current
    production._write_production_manifest = wrapped


def install_runtime_closure() -> None:
    """Install bounded production recovery plus cinematic and delivery stages."""
    install_attempt10_append_bound_recovery()
    install_audio_mastering_live_binding()
    install_sfx_live_binding()
    install_m8_live_binding()
    install_m9_live_binding()
    install_m10_live_binding()
    install_cta_live_binding()
    install_canonical_v4_bundle_post_manifest()


def run_post_gold_observers(output_dir: Path) -> dict:
    """Run G1/G2 only after Gold has accepted the final render.

    This observer is non-authoritative: missing/rate-limited Groq access, transcript
    review, or any audit error never changes Gold or production readiness. The audit
    module writes durable evidence when possible and always returns a document.
    """
    try:
        return run_groq_audio_audit(
            Path(output_dir),
            api_key=_groq_key(),
        )
    except Exception as exc:
        print(f"Runtime post-Gold observer skipped ({type(exc).__name__}); production unchanged")
        return {
            "schema_version": 1,
            "mode": "observe_only",
            "decision": "audit_error",
            "audit_error": f"{type(exc).__name__}: {str(exc)[:240]}",
        }

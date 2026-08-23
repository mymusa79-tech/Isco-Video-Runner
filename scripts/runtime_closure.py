from __future__ import annotations

import os
from pathlib import Path

from scripts.attempt10_append_bound_recovery import install_attempt10_append_bound_recovery
from scripts.audio_mastering_live_binding import install_audio_mastering_live_binding
from scripts.groq_audio_audit import run_groq_audio_audit


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


def install_runtime_closure() -> None:
    """Install bounded pre-production runtime recovery and cinematic audio binding."""
    install_attempt10_append_bound_recovery()
    install_audio_mastering_live_binding()


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

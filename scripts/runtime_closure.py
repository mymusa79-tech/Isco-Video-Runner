from __future__ import annotations

import os
from pathlib import Path

from scripts.attempt10_append_bound_recovery import install_attempt10_append_bound_recovery
from scripts.groq_audio_audit import run_groq_audio_audit


def install_runtime_closure() -> None:
    """Install the remaining pre-production runtime recovery without changing hard gates."""
    install_attempt10_append_bound_recovery()


def run_post_gold_observers(output_dir: Path) -> dict:
    """Run G1/G2 only after Gold has accepted the final render.

    This observer is non-authoritative: missing/rate-limited Groq access, transcript
    review, or any audit error never changes Gold or production readiness. The audit
    module writes durable evidence when possible and always returns a document.
    """
    try:
        return run_groq_audio_audit(
            Path(output_dir),
            api_key=(os.environ.get("GROQ_API_KEY") or "").strip(),
        )
    except Exception as exc:
        print(f"Runtime post-Gold observer skipped ({type(exc).__name__}); production unchanged")
        return {
            "schema_version": 1,
            "mode": "observe_only",
            "decision": "audit_error",
            "audit_error": f"{type(exc).__name__}: {str(exc)[:240]}",
        }

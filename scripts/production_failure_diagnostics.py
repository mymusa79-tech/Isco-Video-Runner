from __future__ import annotations

"""Secret-free failure-domain diagnostics for canonical V4 production failures.

This module is observational only. Text-audit provider availability remains owned by
text_audit_provider_mesh.py; diagnostics consume that owner's typed exception rather
than installing a second audit-outcome policy.
"""

import json
from pathlib import Path

from scripts.text_audit_provider_mesh import TextAuditUnavailableError


SCHEMA_VERSION = 1
_TONE_SEMANTIC_MARKER = "Independent tone/naturalness gate blocked real production"


def is_tone_semantic_failure(exc: Exception) -> bool:
    return not isinstance(exc, TextAuditUnavailableError) and _TONE_SEMANTIC_MARKER in str(exc)


def classify_production_failure(exc: Exception) -> tuple[str, str]:
    """Return stable category/code only; never persist raw exception text."""

    if isinstance(exc, TextAuditUnavailableError):
        return "text_audit", "TEXT_AUDIT_UNAVAILABLE"

    name = type(exc).__name__
    detail = str(exc).lower()
    if "planningstageerror" in name.lower() or "planning_" in detail or "planning stage" in detail:
        return "planning", "PLANNING_FAILURE"
    if "finalmasterqc" in name.lower() or "final master qc" in detail or "gold enforcement" in detail:
        return "final_gold", "FINAL_ACCEPTANCE_FAILURE"
    if "multimodal_injection_firewall" in detail or "security v1" in detail or "security_" in detail:
        return "security", "SECURITY_FAILURE"
    if "visual" in detail or "vision" in detail or "stock candidate" in detail:
        return "visual", "VISUAL_FAILURE"
    if "429" in detail or "quota" in detail or "rate limit" in detail or "ratelimit" in name.lower():
        return "provider", "PROVIDER_CAPACITY_FAILURE"
    if "ffmpeg" in detail or "media" in detail or "audio" in detail or "video" in detail:
        return "media", "MEDIA_FAILURE"
    if is_tone_semantic_failure(exc):
        return "text_audit", "TONE_SEMANTIC_BLOCK"
    return "internal", "UNCLASSIFIED_PRODUCTION_FAILURE"


def write_production_failure_diagnostics(output_dir: Path, exc: Exception) -> Path:
    root = Path(output_dir)
    category, code = classify_production_failure(exc)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "category": category,
        "error_code": code,
        "exception_type": type(exc).__name__,
        "tone_semantic_gate": is_tone_semantic_failure(exc),
        "raw_exception_persisted": False,
    }
    path = root / "production-failure-diagnostics.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path

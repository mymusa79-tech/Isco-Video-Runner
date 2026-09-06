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
_PRODUCER_PATH_MARKER = ":failing_field_paths="
_ALLOWED_PRODUCER_FIELD_PATHS = frozenset(
    {"hook", "sections[0].on_screen_text", "closing_payoff"}
)


def is_tone_semantic_failure(exc: Exception) -> bool:
    return not isinstance(exc, TextAuditUnavailableError) and _TONE_SEMANTIC_MARKER in str(exc)


def _producer_failing_field_paths(exc: Exception) -> list[str]:
    """Extract only allowlisted structural paths; never persist plan/error prose."""
    if "producerqualitycontracterror" not in type(exc).__name__.lower():
        return []
    detail = str(exc)
    if _PRODUCER_PATH_MARKER not in detail:
        return []
    suffix = detail.split(_PRODUCER_PATH_MARKER, 1)[1]
    candidates = [item.strip() for item in suffix.split(",") if item.strip()]
    return [item for item in candidates if item in _ALLOWED_PRODUCER_FIELD_PATHS]


def classify_production_failure(exc: Exception) -> tuple[str, str]:
    """Return stable category/code only; never persist raw exception text."""

    if isinstance(exc, TextAuditUnavailableError):
        return "text_audit", "TEXT_AUDIT_UNAVAILABLE"

    name = type(exc).__name__
    detail = str(exc).lower()
    # Run217: ProducerQualityContractError is a deterministic planning acceptance block,
    # not an unexpected internal crash. Classify by exception type so diagnostics remain
    # secret-free and do not depend on persisting the raw plan/error text.
    if "producerqualitycontracterror" in name.lower():
        return "planning", "PRODUCER_PLAN_QUALITY_BLOCK"
    # Run200: a technical Vision outage means no semantic verdict was made. Keep it in
    # the visual stage domain but give it its own stable availability code instead of
    # collapsing it into generic VISUAL_FAILURE / candidate exhaustion.
    if "visionverdictunavailableerror" in name.lower() or "vision_unavailable" in detail:
        return "visual", "VISION_UNAVAILABLE"
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
    producer_paths = _producer_failing_field_paths(exc)
    if producer_paths:
        payload["producer_failing_field_paths"] = producer_paths
    path = root / "production-failure-diagnostics.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path

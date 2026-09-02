from __future__ import annotations

"""Preserve technical-vs-semantic text-audit outcomes at the production boundary.

Engine text_audit_router already distinguishes a real semantic verdict from provider
exhaustion. The legacy audit adapters intentionally fail closed by returning
``status=block`` on technical failure, but RepairDossier cannot tell that synthetic
block from a real content defect and may rewrite otherwise-good content. This Runner
adapter keeps fail-closed behavior while restoring the missing failure-domain
identity: technical inability to obtain a verdict raises before RepairDossier, while
real provider ``pass``/``block`` verdicts remain byte-for-byte authoritative.
"""

from functools import wraps
from typing import Any, Callable

import isco_video_agent.orchestrator as orchestrator


PROVIDER_EXHAUSTED = "TEXT_AUDIT_PROVIDER_EXHAUSTED"
INVALID_MODEL_OUTPUT = "TEXT_AUDIT_INVALID_MODEL_OUTPUT"
_INSTALLED = False


class TextAuditOutcomeError(RuntimeError):
    """Stable, secret-free technical failure emitted before content repair."""

    def __init__(self, code: str, dimension: str):
        self.code = str(code)
        self.dimension = str(dimension)
        super().__init__(f"{self.code} dimension={self.dimension}")


def _validation_state(result: object, diagnostics: object = None) -> str | None:
    for candidate in (diagnostics, result):
        if isinstance(candidate, dict):
            value = str(candidate.get("validation") or "").strip().lower()
            if value:
                return value
    return None


def enforce_text_audit_outcome(
    dimension: str,
    result: object,
    *,
    diagnostics: object = None,
):
    """Raise only for a technical audit failure; preserve semantic verdicts unchanged."""

    validation = _validation_state(result, diagnostics)
    if validation == "providers_exhausted":
        raise TextAuditOutcomeError(PROVIDER_EXHAUSTED, dimension)
    if validation == "malformed":
        raise TextAuditOutcomeError(INVALID_MODEL_OUTPUT, dimension)
    return result


def _wrap_audit(
    original: Callable[..., Any],
    *,
    dimension: str,
    factuality_diagnostics: bool = False,
) -> Callable[..., Any]:
    if getattr(original, "_isco_text_audit_outcome_v1", False):
        return original

    @wraps(original)
    def wrapped(*args, **kwargs):
        diagnostics = None
        call_kwargs = kwargs
        if factuality_diagnostics:
            # Production already supplies this dict. Supplying one for any direct
            # caller is backwards-compatible with Engine audit_plan's keyword-only
            # diagnostics seam and prevents technical exhaustion from becoming an
            # indistinguishable synthetic content block outside orchestrator too.
            diagnostics = kwargs.get("diagnostics")
            if diagnostics is None:
                diagnostics = {}
                call_kwargs = dict(kwargs)
                call_kwargs["diagnostics"] = diagnostics
        result = original(*args, **call_kwargs)
        return enforce_text_audit_outcome(
            dimension,
            result,
            diagnostics=diagnostics,
        )

    wrapped._isco_text_audit_outcome_v1 = True
    wrapped._isco_text_audit_original = original
    return wrapped


def install_text_audit_outcome_contract() -> None:
    """Install after routing/runtime patches, before Producer/RepairDossier execution."""

    global _INSTALLED
    if _INSTALLED:
        return
    orchestrator.audit_plan = _wrap_audit(
        orchestrator.audit_plan,
        dimension="factuality",
        factuality_diagnostics=True,
    )
    orchestrator.audit_semantic_repetition = _wrap_audit(
        orchestrator.audit_semantic_repetition,
        dimension="semantic_repetition",
    )
    orchestrator.audit_tone_and_naturalness = _wrap_audit(
        orchestrator.audit_tone_and_naturalness,
        dimension="tone",
    )
    _INSTALLED = True
    print(
        "Text Audit Outcome Contract installed: semantic_pass_block_preserved=true "
        "provider_exhaustion_is_technical=true malformed_is_technical=true"
    )

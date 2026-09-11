from __future__ import annotations

"""Compatibility shim for Run #238 across the current and future Engine pins.

The currently certified production Engine predates the Engine-side no-wire accounting
contract. Runner owns the local capacity precheck, so it must also prove that an
explicit pre-wire failure neither consumes a Provider Attempt nor opens a run-scoped
rate-limit circuit. Newer Engines already implement the same invariant; these wrappers
remain transparent there.
"""

from functools import wraps

import isco_video_agent.text_audit_router as engine_audit_router
from isco_video_agent.ai_budget import AttemptOutcome


_NO_WIRE_MARKERS = (
    "NO_WIRE_CAPACITY_FAILOVER",
    "NO_WIRE_LOCAL_FAILURE",
)
_INSTALLED = False


def _is_no_wire_exception(exc: BaseException) -> bool:
    return getattr(exc, "wire_attempted", None) is False


def _is_no_wire_detail(detail: object) -> bool:
    text = str(detail or "")
    return any(marker in text for marker in _NO_WIRE_MARKERS)


def install_text_audit_no_wire_compat() -> None:
    """Install after Text Audit Provider Mesh so all ledger routes pass through it.

    This installer is composition-idempotent rather than one-shot. Test/diagnostic
    processes may replace the Engine recorder after an earlier install; in that case we
    must wrap the *current* recorder again. Marker checks prevent duplicate wrapping
    when the current composition is already correct.
    """
    global _INSTALLED
    changed = False

    current_classify = engine_audit_router._classify_exception
    if not getattr(current_classify, "_isco_run238_no_wire_classifier", False):
        @wraps(current_classify)
        def classify(exc: Exception):
            # Local admission/preflight is a technical route event, never a provider
            # rate-limit response. Returning OTHER guarantees the legacy router advances
            # without poisoning its run-scoped quota/rate-limit circuit.
            if _is_no_wire_exception(exc):
                return AttemptOutcome.OTHER
            return current_classify(exc)

        classify._isco_run238_no_wire_classifier = True
        classify._isco_original = current_classify
        engine_audit_router._classify_exception = classify
        changed = True

    current_record = engine_audit_router._record_wire_attempt
    if not getattr(current_record, "_isco_run238_wire_only_recording", False):
        @wraps(current_record)
        def record(
            provider: str,
            outcome,
            *,
            duration_seconds: float,
            detail: str | None = None,
        ) -> None:
            # Engine <= current production pin calls its recorder even when Runner's
            # callable failed before HTTP. Explicit marker proof is the only exemption;
            # every unknown/unmarked failure stays fail-closed and counted.
            if _is_no_wire_detail(detail):
                return
            return current_record(
                provider,
                outcome,
                duration_seconds=duration_seconds,
                detail=detail,
            )

        record._isco_run238_wire_only_recording = True
        record._isco_original = current_record
        engine_audit_router._record_wire_attempt = record
        changed = True

    _INSTALLED = True
    if changed:
        print(
            "Text Audit no-wire compatibility installed: "
            "legacy_engine_counting=guarded rate_limit_circuit=no_wire_exempt "
            "new_engine_compatible=true composition_idempotent=true"
        )

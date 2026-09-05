from __future__ import annotations

"""Run #200 preventive closure for Short Cinematic Vision availability.

The failure family is narrower than candidate retrieval: a Short beat can have hundreds
of locally eligible stock candidates yet still fail when the one cloud adjudication slot
lands inside a transient Vision-provider cooldown. The selector correctly refuses to
pretend that a technical failure is a semantic PASS, but historically the Short caller
then collapsed the unmade verdict into "no safe distinct asset" and stopped.

This closure keeps all existing visual/security thresholds and candidate caps. It adds
only three availability/forensics behaviors at the Short finishing seam:

1. If the canonical Vision mesh returns a *technical-unavailable* envelope and the
   existing Groq capacity owner has a live, bounded 429 cooldown, wait through that
   existing owner and half-open exactly once on the SAME candidate. A technical failure
   never consumes approval authority and never triggers candidate/provider shopping.
2. If that bounded half-open still cannot produce a semantic verdict, reuse the existing
   Run183 truthful-outcome guard so the terminal error is VISION_UNAVAILABLE rather than
   the false "no safe asset" diagnosis.
3. Persist each selector result atomically as partial Short visual evidence before any
   terminal exception, so a failed beat remains diagnosable.

The retry is intentionally one-per-beat-factory, provider-attempt accounting remains in
BudgetLedger/Stage Contract, and the run-wide hard cap remains authoritative.
"""

import json
import time
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from typing import Any

from scripts import opening_feasibility_guard as opening_guard
from scripts import provider_health_registry as health
from scripts import run181_vision_mesh_closure as run181
from scripts import short_cinematic_director as short_director
from scripts import short_voice_owned_timeline as short_voice
from scripts import visual_retrieval_adjudication_v1 as visual_v1


CONTRACT_ID = "run200-short-vision-recovery-v1"
MAX_HALF_OPEN_WAIT_SECONDS = float(visual_v1.GROQ_MAX_BOUNDED_WAIT_SECONDS)
PARTIAL_AUDIT_FILENAME = "short-cinematic-visual-audit.partial.json"

_INSTALLED = False
_ACTIVE_ROOT: ContextVar[Path | None] = ContextVar(
    "isco_run200_short_visual_root",
    default=None,
)
_SELECTOR_CALL_INDEX: ContextVar[int] = ContextVar(
    "isco_run200_short_selector_call_index",
    default=0,
)


def _is_technical_unavailable(payload: object) -> bool:
    return bool(
        isinstance(payload, dict)
        and payload.get("verdict_authority") == "technical_unavailable"
        and payload.get("vision_review_performed") is False
        and payload.get("semantic_verdict") is False
    )


def _active_groq_cooldown_seconds() -> float | None:
    """Return only an already-owned live HTTP-429 cooldown; never invent a new sleep."""
    state = visual_v1._capacity_state()
    if int(state.last_status or 0) != 429:
        return None
    remaining = max(0.0, float(state.next_allowed_monotonic) - time.monotonic())
    if remaining <= 0.01 or remaining > MAX_HALF_OPEN_WAIT_SECONDS:
        return None
    return remaining


def _install_exact_groq_cooldown_preservation() -> None:
    """Do not overwrite a server/header-derived cooldown with the legacy 60s fallback.

    Visual Retrieval V1 already observes Groq's HTTP response headers before Run181
    publishes provider-health evidence. Its historical publisher wrapper then parses the
    shortened exception string and can conservatively extend an exact Retry-After/reset
    window to 60 seconds. Preserve the already-known live window instead; external
    telemetry with no observed HTTP window still falls through to the certified V1
    fallback behavior.
    """
    current = health.publish_provider_unavailable
    if getattr(current, "_isco_run200_exact_groq_cooldown", False):
        return

    @wraps(current)
    def wrapped(
        provider: str,
        *,
        model: str = "*",
        quota_domain: str = "*",
        reason: str,
        source: str,
    ):
        if (
            str(provider).strip().lower() == "groq"
            and str(model).strip() == run181.GROQ_VISION_MODEL
            and run181._quota_or_rate_failure(reason)
            and not visual_v1._is_daily_groq_limit(reason)
        ):
            wait = _active_groq_cooldown_seconds()
            if wait is not None:
                print(
                    "Run200 Vision recovery: preserving observed Groq 429 cooldown "
                    f"seconds={wait:.2f} source={source}"
                )
                return None
        return current(
            provider,
            model=model,
            quota_domain=quota_domain,
            reason=reason,
            source=source,
        )

    wrapped._isco_run200_exact_groq_cooldown = True
    wrapped._isco_run200_original = current
    health.publish_provider_unavailable = wrapped


def _install_short_same_candidate_half_open() -> None:
    current_factory = short_director._stable_intent_audit
    if getattr(current_factory, "_isco_run200_short_half_open", False):
        return

    @wraps(current_factory)
    def recovering_factory(audit_fn, intended_visual: str):
        base = current_factory(audit_fn, intended_visual)
        recovery_used = False

        @wraps(base)
        def wrapped(*args, **kwargs):
            nonlocal recovery_used
            first = base(*args, **kwargs)
            if recovery_used or not _is_technical_unavailable(first):
                return first
            wait = _active_groq_cooldown_seconds()
            if wait is None:
                return first

            recovery_used = True
            print(
                "Run200 Vision recovery: bounded half-open retry on same Short candidate "
                f"after Groq cooldown seconds={wait:.2f}; candidate/retrieval budgets unchanged"
            )
            # Do not sleep here. The already-certified Groq capacity owner at the HTTP
            # boundary owns the exact wait and will block this second call until its
            # observed Retry-After/reset deadline. This keeps one timing owner.
            return base(*args, **kwargs)

        return wrapped

    recovering_factory._isco_run200_short_half_open = True
    recovering_factory._isco_run200_original = current_factory
    short_director._stable_intent_audit = recovering_factory


def _audit_rows(result: object, *, selector_index: int, intended_visual: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for review in list(getattr(result, "reviewed", ()) or ()):
        audit = getattr(review, "audit", None)
        if not isinstance(audit, dict):
            continue
        candidate = getattr(review, "candidate", None)
        candidate_id = candidate.get("id") if isinstance(candidate, dict) else None
        rows.append(
            {
                "selector_index": int(selector_index),
                "provider": str(getattr(review, "provider", "") or ""),
                "candidate_id": candidate_id,
                "from_cache": bool(getattr(review, "from_cache", False)),
                "intended_visual": str(intended_visual or "")[:500],
                "selector_status": str(getattr(result, "status", "")),
                "used_alternate_query": bool(getattr(result, "used_alternate_query", False)),
                **dict(audit),
            }
        )
    return rows


def _persist_partial_result(result: object, *, intended_visual: str) -> None:
    root = _ACTIVE_ROOT.get()
    if root is None:
        return
    index = _SELECTOR_CALL_INDEX.get() + 1
    _SELECTOR_CALL_INDEX.set(index)
    path = Path(root) / PARTIAL_AUDIT_FILENAME
    try:
        existing: list[dict[str, Any]] = []
        if path.is_file():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                existing = [row for row in loaded if isinstance(row, dict)]
        existing.extend(_audit_rows(result, selector_index=index, intended_visual=intended_visual))
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)
    except Exception as exc:
        # Forensics must never mask the production verdict.
        print(
            "Run200 Vision recovery: partial Short visual audit persistence skipped "
            f"({type(exc).__name__})"
        )


def _install_truthful_short_selector_outcome() -> None:
    current = short_director.select_with_recovery
    if getattr(current, "_isco_run200_truthful_short_outcome", False):
        return

    @wraps(current)
    def wrapped(*args, **kwargs):
        result = current(*args, **kwargs)
        intended_visual = str(kwargs.get("intended_visual") or "")
        _persist_partial_result(result, intended_visual=intended_visual)
        # Existing Run183 helper changes no PASS/BLOCK rule. It only prevents a final
        # technically-unjudged selector result from masquerading as asset exhaustion.
        return opening_guard._enforce_truthful_visual_outcome(
            result,
            scope="short_cinematic",
        )

    wrapped._isco_run200_truthful_short_outcome = True
    wrapped._isco_run200_original = current
    short_director.select_with_recovery = wrapped


def _install_short_root_scope() -> None:
    current = short_voice.upgrade_short_cinematic
    if getattr(current, "_isco_run200_short_visual_root", False):
        return

    @wraps(current)
    def wrapped(root: Path, *args, **kwargs):
        active = _ACTIVE_ROOT.get()
        if active is not None:
            return current(root, *args, **kwargs)
        resolved = Path(root)
        token_root = _ACTIVE_ROOT.set(resolved)
        token_index = _SELECTOR_CALL_INDEX.set(0)
        partial = resolved / PARTIAL_AUDIT_FILENAME
        try:
            # A production output directory is run-specific. Start this invocation with
            # a clean partial stream so resume/test residue cannot be mistaken for live
            # evidence.
            partial.unlink(missing_ok=True)
            return current(root, *args, **kwargs)
        finally:
            _SELECTOR_CALL_INDEX.reset(token_index)
            _ACTIVE_ROOT.reset(token_root)

    wrapped._isco_run200_short_visual_root = True
    wrapped._isco_run200_original = current
    short_voice.upgrade_short_cinematic = wrapped


def install_run200_short_vision_recovery_closure() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _install_exact_groq_cooldown_preservation()
    _install_short_same_candidate_half_open()
    _install_truthful_short_selector_outcome()
    _install_short_root_scope()
    _INSTALLED = True
    print(
        "Run200 Short Vision recovery installed: same-candidate half-open=once; "
        "cooldown_owner=existing Groq header-aware capacity; truthful_failure=VISION_UNAVAILABLE; "
        "partial_visual_audit=atomic; visual/security thresholds and retrieval caps unchanged"
    )

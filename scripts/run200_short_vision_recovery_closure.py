from __future__ import annotations

"""Run #200 preventive closure for Short Cinematic visual/Vision reliability.

The Short finishing seam runs after ``orchestrator.produce()`` and therefore must
explicitly re-enter the canonical Visual Retrieval runtime scope. Run200 adds one bounded
same-candidate availability half-open after an already-observed Groq 429, preserves the
server/header cooldown, keeps semantic/security verdicts authoritative, and reports an
unmade technical verdict as ``VISION_UNAVAILABLE`` rather than false stock exhaustion.

All composition in this module is request-scoped and restored in ``finally``. No process-
lifetime monkey patch is allowed to escape the Short finishing seam.
"""

import json
import time
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from typing import Any, Iterator

import isco_video_agent.orchestrator as orchestrator
import isco_video_agent.visual_selection as visual_selection
from scripts import opening_feasibility_guard as opening_guard
from scripts import provider_health_registry as health
from scripts import run181_vision_mesh_closure as run181
from scripts import short_cinematic_director as short_director
from scripts import visual_retrieval_adjudication_v1 as visual_v1
from scripts import visual_retrieval_runtime_scope_v1 as visual_scope


CONTRACT_ID = "run200-short-visual-runtime-recovery-v3"
MAX_HALF_OPEN_WAIT_SECONDS = float(visual_v1.GROQ_MAX_BOUNDED_WAIT_SECONDS)
PARTIAL_AUDIT_FILENAME = "short-cinematic-visual-audit.partial.json"

_ACTIVE_ROOT: ContextVar[Path | None] = ContextVar("isco_run200_short_visual_root", default=None)
_SELECTOR_CALL_INDEX: ContextVar[int] = ContextVar("isco_run200_short_selector_call_index", default=0)


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


def _make_exact_groq_cooldown_publisher(current):
    """Preserve a header-derived cooldown instead of publishing a generic 60s fallback."""

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
    return wrapped


def _make_short_same_candidate_half_open(current_factory):
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
            # The canonical Groq transport owns the wait. Re-entering the same audit only
            # gives that owner one half-open opportunity; this layer never sleeps itself.
            second = base(*args, **kwargs)
            if isinstance(second, dict):
                second = dict(second)
                second["availability_recovery_attempted"] = True
                second["availability_recovery_wait_seconds"] = round(float(wait), 3)
                second["availability_recovery_first_reason"] = str(first.get("reason") or "")[:300]
            return second

        return wrapped

    recovering_factory._isco_run200_short_half_open = True
    recovering_factory._isco_run200_original = current_factory
    return recovering_factory


def _audit_rows(result: object, *, selector_index: int, intended_visual: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for review in list(getattr(result, "reviewed", ()) or ()):
        audit = getattr(review, "audit", None)
        if not isinstance(audit, dict):
            continue
        candidate = getattr(review, "candidate", None)
        candidate_id = candidate.get("id") if isinstance(candidate, dict) else None
        candidate_url = None
        candidate_duration = None
        if isinstance(candidate, dict):
            candidate_url = candidate.get("url") or candidate.get("pageURL")
            candidate_duration = candidate.get("duration")
        rows.append(
            {
                "selector_index": int(selector_index),
                "provider": str(getattr(review, "provider", "") or ""),
                "candidate_id": candidate_id,
                "candidate_url": candidate_url,
                "candidate_duration_seconds": candidate_duration,
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
        print(
            "Run200 Vision recovery: partial Short visual audit persistence skipped "
            f"({type(exc).__name__})"
        )


def _make_truthful_short_selector(current):
    @wraps(current)
    def wrapped(*args, **kwargs):
        result = current(*args, **kwargs)
        intended_visual = str(kwargs.get("intended_visual") or "")
        _persist_partial_result(result, intended_visual=intended_visual)
        return opening_guard._enforce_truthful_visual_outcome(result, scope="short_cinematic")

    wrapped._isco_run200_truthful_short_outcome = True
    wrapped._isco_run200_original = current
    return wrapped


@contextmanager
def short_vision_recovery_scope(root: Path) -> Iterator[None]:
    """Temporarily compose canonical Visual V1 policy into the Short finishing seam."""
    if _ACTIVE_ROOT.get() is not None:
        # Re-entrant callers reuse the outer request-scoped bindings and ContextVars.
        with visual_scope.visual_retrieval_runtime_scope():
            yield
        return

    resolved = Path(root)
    token_root = _ACTIVE_ROOT.set(resolved)
    token_index = _SELECTOR_CALL_INDEX.set(0)
    original_health = health.publish_provider_unavailable
    original_selector = short_director.select_with_recovery
    original_audit = short_director._stable_intent_audit
    original_pexels = short_director.pexels_search_videos
    partial = resolved / PARTIAL_AUDIT_FILENAME

    try:
        partial.unlink(missing_ok=True)
        with visual_scope.visual_retrieval_runtime_scope():
            # These are imported-by-value Short surfaces. Bind them only for this request
            # and restore every identity afterward, including exception paths.
            health.publish_provider_unavailable = _make_exact_groq_cooldown_publisher(original_health)
            short_director.select_with_recovery = _make_truthful_short_selector(
                visual_selection.select_with_recovery
            )
            short_director._stable_intent_audit = _make_short_same_candidate_half_open(
                opening_guard._stable_intent_audit
            )
            short_director.pexels_search_videos = orchestrator.pexels_search_videos
            yield
    finally:
        health.publish_provider_unavailable = original_health
        short_director.select_with_recovery = original_selector
        short_director._stable_intent_audit = original_audit
        short_director.pexels_search_videos = original_pexels
        _SELECTOR_CALL_INDEX.reset(token_index)
        _ACTIVE_ROOT.reset(token_root)


def install_run200_short_vision_recovery_closure() -> None:
    """Compatibility shim: Run200 policy is now activated only by the scoped seam."""
    return None

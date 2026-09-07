from __future__ import annotations

"""Gold Vision capacity reserve shared by Long, standalone Short and sibling Short.

Visual Retrieval & Adjudication V1 remains the sole owner of Groq header parsing and
TPM pacing.  This module composes one release-priority rule above that owner:

* when Gemini and OpenRouter are already unavailable, non-Gold Groq calls preserve
  enough of the current token window for one terminal Gold visual audit;
* the Gold Groq route may honor the already-observed provider cooldown once;
* every physical retry still passes through the existing BudgetLedger authorizer and
  recorder; no provider attempt is hidden;
* no semantic/quality/security threshold is changed.
"""

import math
import time
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

from scripts import gold_final_critic_text_fallback as gold_fallback
from scripts import provider_health_registry as health
from scripts import run181_vision_mesh_closure as vision_mesh
from scripts import vision_stage_contract_v2 as vision_contract
from scripts import visual_retrieval_adjudication_v1 as capacity


CONTRACT_ID = "gold-vision-capacity-reserve-v1"
CONTRACT_VERSION = 1
GOLD_RESERVE_FRACTION = 0.80
GOLD_RESERVE_MIN_TOKENS = 3200
GOLD_RESERVE_MAX_TOKENS = 4200
GOLD_GROQ_RETRY_MAX_WAIT_SECONDS = 65.0

_GOLD_ACTIVE: ContextVar[bool] = ContextVar("isco_gold_vision_capacity_active", default=False)
_INSTALLED = False


def _gold_reserve_tokens(estimated_tokens: int) -> int:
    estimate = max(1, int(estimated_tokens))
    reserve = int(math.ceil(float(estimate) * GOLD_RESERVE_FRACTION))
    return max(GOLD_RESERVE_MIN_TOKENS, min(GOLD_RESERVE_MAX_TOKENS, reserve))


def _openrouter_unavailable() -> bool:
    return health.provider_unavailable(
        "openrouter",
        model=vision_contract.OPENROUTER_PRIMARY_MODEL,
        quota_domain="vision",
    ) is not None


def _gemini_unavailable() -> bool:
    return health.provider_unavailable(
        "gemini",
        model=vision_mesh._gemini_runtime_model(),
        quota_domain=vision_mesh.GEMINI_GENERATION_QUOTA_DOMAIN,
    ) is not None


def _groq_is_last_live_vision_provider() -> bool:
    return _gemini_unavailable() and _openrouter_unavailable()


def _install_reserve_admission() -> None:
    current = capacity._admit_groq
    if getattr(current, "_isco_gold_capacity_reserve_v1", False):
        return

    @wraps(current)
    def reserve_aware_admit(estimated_tokens: int) -> None:
        state = capacity._capacity_state()
        estimate = max(1, int(estimated_tokens))
        if not _GOLD_ACTIVE.get() and _groq_is_last_live_vision_provider():
            remaining = state.remaining_tokens
            reset = state.reset_tokens_seconds
            reserve = _gold_reserve_tokens(estimate)
            if (
                remaining is not None
                and reset is not None
                and remaining < estimate + reserve
                and reset > 0.01
            ):
                bounded = min(float(reset), capacity.GROQ_MAX_BOUNDED_WAIT_SECONDS)
                print(
                    "Gold Vision Capacity Reserve V1: preserving terminal Groq window "
                    f"remaining_tokens={remaining} next_estimate={estimate} "
                    f"gold_reserve={reserve} wait_seconds={bounded:.2f}"
                )
                time.sleep(bounded)
                # Only discard the stale token count when the complete provider reset
                # interval was actually honored.  Any longer cooldown remains owned by
                # the existing V1 next_allowed_monotonic boundary.
                if float(reset) <= capacity.GROQ_MAX_BOUNDED_WAIT_SECONDS:
                    state.remaining_tokens = None
                    state.reset_tokens_seconds = None
        return current(estimate)

    reserve_aware_admit._isco_gold_capacity_reserve_v1 = True
    reserve_aware_admit._isco_gold_capacity_original = current
    capacity._admit_groq = reserve_aware_admit


def _install_gold_groq_retry() -> None:
    current = vision_mesh._run_groq_attempt
    if getattr(current, "_isco_gold_groq_retry_v1", False):
        return

    @wraps(current)
    def gold_retry_once(*args, **kwargs):
        try:
            return current(*args, **kwargs)
        except vision_contract.VisionStageError as exc:
            if (
                not _GOLD_ACTIVE.get()
                or exc.code is not vision_contract.VisionErrorCode.PROVIDER_TRANSIENT
                or not vision_mesh._quota_or_rate_failure(exc.detail)
            ):
                raise
            state = capacity._capacity_state()
            delay = max(0.0, float(state.next_allowed_monotonic) - time.monotonic())
            if delay <= 0.01 or delay > GOLD_GROQ_RETRY_MAX_WAIT_SECONDS:
                raise
            print(
                "Gold Vision Capacity Reserve V1: honoring Groq provider cooldown once; "
                f"delay_seconds={delay:.3f}"
            )
            time.sleep(delay)
            # `current` performs a fresh ledger authorization + record, so the retry is
            # a truthful physical provider attempt rather than a hidden transport loop.
            return current(*args, **kwargs)

    gold_retry_once._isco_gold_groq_retry_v1 = True
    gold_retry_once._isco_gold_groq_retry_original = current
    vision_mesh._run_groq_attempt = gold_retry_once


def _install_gold_scope() -> None:
    # Gold Phase 4 imported the context manager by name, so bind the scope at that exact
    # consumer rather than replacing provider or semantic ownership globally.
    from scripts import gold_enforce_phase4 as gold_enforce

    current = gold_enforce.gold_final_critic_text_fallback
    if getattr(current, "_isco_gold_capacity_scope_v1", False):
        return

    @contextmanager
    def scoped_gold_fallback():
        token = _GOLD_ACTIVE.set(True)
        try:
            with current():
                yield
        finally:
            _GOLD_ACTIVE.reset(token)

    scoped_gold_fallback._isco_gold_capacity_scope_v1 = True
    scoped_gold_fallback._isco_gold_capacity_scope_original = current
    gold_enforce.gold_final_critic_text_fallback = scoped_gold_fallback


def _expand_truthful_gold_attempt_budget() -> None:
    # Existing policy allows Gemini + provider-directed Gemini retry + Groq +
    # OpenRouter = four physical Vision attempts.  This closure adds at most one
    # provider-directed Groq retry, therefore the explicit Vision ceiling becomes five.
    gold_fallback._FINAL_CRITIC_VISION_MAX_PROVIDER_ATTEMPTS = max(
        5,
        int(gold_fallback._FINAL_CRITIC_VISION_MAX_PROVIDER_ATTEMPTS),
    )
    gold_fallback._FINAL_CRITIC_TOTAL_PROVIDER_ATTEMPTS = (
        int(gold_fallback._FINAL_CRITIC_VISION_MAX_PROVIDER_ATTEMPTS)
        + int(gold_fallback._FINAL_CRITIC_TEXT_MAX_PROVIDER_ATTEMPTS)
    )


def install_gold_vision_capacity_reserve_v1() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _expand_truthful_gold_attempt_budget()
    _install_reserve_admission()
    _install_gold_groq_retry()
    _install_gold_scope()
    _INSTALLED = True
    print(
        "Gold Vision Capacity Reserve V1 installed: shared Long+Short terminal reserve; "
        "last-live-provider protection; one bounded Groq provider cooldown retry; "
        "Gold physical Vision cap=5; semantic/quality/security gates unchanged"
    )

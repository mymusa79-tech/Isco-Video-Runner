from __future__ import annotations

"""Gold Vision priority-admission policy shared by Long and Shorts.

Visual Retrieval & Adjudication V1 remains the sole owner of Groq header parsing and
TPM pacing. This module does not reserve provider capacity at Groq; it applies a local
priority-admission margin so non-Gold calls avoid consuming the last observed token
window when Gemini and OpenRouter are already unavailable.

The one provider-directed Groq retry is exposed truthfully to BudgetLedger and the
expanded physical-attempt allowance exists only while the Gold fallback context is
active. No semantic, quality, or security threshold is changed.
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
from scripts.gold_text_qc_pending_v1 import install_gold_text_qc_pending_v1
from scripts.telegram_progress import update_stage
from scripts.vision_provider_failure_unification_v1 import (
    gold_over_capacity_cooldown_scope,
    install_vision_provider_failure_unification_v1,
)


CONTRACT_ID = "gold-vision-capacity-reserve-v1"
CONTRACT_VERSION = 1
POLICY_NAME = "gold-vision-priority-admission-v1"
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


@contextmanager
def _scoped_gold_attempt_budget():
    """Temporarily allow one truthful provider-directed Groq retry during Gold only."""
    before_vision = int(gold_fallback._FINAL_CRITIC_VISION_MAX_PROVIDER_ATTEMPTS)
    before_total = int(gold_fallback._FINAL_CRITIC_TOTAL_PROVIDER_ATTEMPTS)
    try:
        gold_fallback._FINAL_CRITIC_VISION_MAX_PROVIDER_ATTEMPTS = max(5, before_vision)
        gold_fallback._FINAL_CRITIC_TOTAL_PROVIDER_ATTEMPTS = (
            int(gold_fallback._FINAL_CRITIC_VISION_MAX_PROVIDER_ATTEMPTS)
            + int(gold_fallback._FINAL_CRITIC_TEXT_MAX_PROVIDER_ATTEMPTS)
        )
        yield
    finally:
        gold_fallback._FINAL_CRITIC_VISION_MAX_PROVIDER_ATTEMPTS = before_vision
        gold_fallback._FINAL_CRITIC_TOTAL_PROVIDER_ATTEMPTS = before_total


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
                    "Gold Vision Priority Admission V1: delaying non-Gold Groq call "
                    f"remaining_tokens={remaining} next_estimate={estimate} "
                    f"priority_margin={reserve} wait_seconds={bounded:.2f}"
                )
                time.sleep(bounded)
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
                "Gold Vision Priority Admission V1: honoring Groq cooldown once; "
                f"delay_seconds={delay:.3f}"
            )
            update_stage("provider_wait")
            time.sleep(delay)
            update_stage("gold_vision")
            return current(*args, **kwargs)

    gold_retry_once._isco_gold_groq_retry_v1 = True
    gold_retry_once._isco_gold_groq_retry_original = current
    vision_mesh._run_groq_attempt = gold_retry_once


def _install_gold_scope() -> None:
    from scripts import gold_enforce_phase4 as gold_enforce

    current = gold_enforce.gold_final_critic_text_fallback
    if getattr(current, "_isco_gold_capacity_scope_v1", False):
        return

    @contextmanager
    def scoped_gold_fallback():
        token = _GOLD_ACTIVE.set(True)
        try:
            with (
                gold_over_capacity_cooldown_scope(),
                _scoped_gold_attempt_budget(),
                current(),
            ):
                yield
        finally:
            _GOLD_ACTIVE.reset(token)

    scoped_gold_fallback._isco_gold_capacity_scope_v1 = True
    scoped_gold_fallback._isco_gold_capacity_scope_original = current
    gold_enforce.gold_final_critic_text_fallback = scoped_gold_fallback


def install_gold_vision_capacity_reserve_v1() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    # Canonical Production calls this before any actual Vision work, and the Gold-only
    # resume path installs the same owner. Keep provider-failure taxonomy composition at
    # this shared Long+Short seam so ordinary production and deferred Gold use exactly
    # the same transport semantics, while Gold-only saturation policy remains scoped.
    install_vision_provider_failure_unification_v1()
    install_gold_text_qc_pending_v1()
    _install_reserve_admission()
    _install_gold_groq_retry()
    _install_gold_scope()
    _INSTALLED = True
    print(
        "Gold Vision Priority Admission V1 installed: shared Long+Short local capacity margin; "
        "one bounded provider-directed Groq retry; Gold-only physical Vision cap=5; "
        "semantic/quality/security gates unchanged"
    )

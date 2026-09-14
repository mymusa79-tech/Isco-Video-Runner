from __future__ import annotations

"""Gold Vision priority-admission policy shared by Long and Shorts.

Provider Capacity Hardening is the sole owner of Groq header parsing, persisted model
state, and TPM pacing. This module contributes only Gold priority: both the final
non-Gold calls (when Groq is the last live route) and Gold itself require an additional
admission cushion before any wire call.

The one provider-directed Groq retry is exposed truthfully to BudgetLedger and the
expanded physical-attempt allowance exists only while the Gold fallback context is
active. No semantic, quality, or security threshold is changed.
"""

import math
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

from scripts import gold_final_critic_text_fallback as gold_fallback
from scripts import provider_capacity_hardening as shared_capacity
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
GOLD_VISION_PHYSICAL_ATTEMPT_CAP = 6

_GOLD_ACTIVE: ContextVar[bool] = ContextVar("isco_gold_vision_capacity_active", default=False)
_GOLD_GROQ_RETRY_SPENT: ContextVar[bool] = ContextVar(
    "isco_gold_vision_groq_retry_spent",
    default=False,
)
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
        gold_fallback._FINAL_CRITIC_VISION_MAX_PROVIDER_ATTEMPTS = max(
            GOLD_VISION_PHYSICAL_ATTEMPT_CAP,
            before_vision,
        )
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
    def reserve_aware_admit(
        estimated_tokens: int,
        *,
        reserve_tokens: int = 0,
    ) -> float:
        estimate = max(1, int(estimated_tokens))
        needs_gold_cushion = _GOLD_ACTIVE.get() or _groq_is_last_live_vision_provider()
        policy_reserve = _gold_reserve_tokens(estimate) if needs_gold_cushion else 0
        # Keep the strongest request when a targeted retry explicitly carries the same
        # Gold cushion through this wrapper. Never add both values and accidentally make
        # an otherwise feasible request exceed the provider's real TPM ceiling.
        reserve = max(0, int(reserve_tokens), policy_reserve)
        return current(estimate, reserve_tokens=reserve)

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
                or _GOLD_GROQ_RETRY_SPENT.get()
                or exc.code is not vision_contract.VisionErrorCode.PROVIDER_TRANSIENT
                or not vision_mesh._quota_or_rate_failure(exc.detail)
            ):
                raise
            state = shared_capacity.groq_capacity_snapshot(vision_mesh.GROQ_VISION_MODEL)
            if state.get("blocked_reason"):
                raise
            estimate = state.get("last_estimated_tokens")
            if not isinstance(estimate, int) or estimate <= 0:
                raise
            decision = shared_capacity.groq_capacity_pacing_decision(
                vision_mesh.GROQ_VISION_MODEL,
                estimate,
                reserve_tokens=_gold_reserve_tokens(estimate),
            )
            wait_until = decision.get("wait_until_epoch")
            if decision.get("action") != "wait" or not isinstance(wait_until, (int, float)):
                raise
            _GOLD_GROQ_RETRY_SPENT.set(True)
            print(
                "Gold Vision Priority Admission V1: honoring one shared-ledger Groq "
                f"cooldown; reason={decision['reason']}"
            )
            update_stage("provider_wait")
            try:
                try:
                    capacity._admit_groq(
                        estimate,
                        reserve_tokens=_gold_reserve_tokens(estimate),
                    )
                except Exception as wait_error:
                    # Preserve the typed provider failure expected by the Vision mesh;
                    # a no-wire wait-budget refusal must not escape as an internal error.
                    raise exc from wait_error
            finally:
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
        retry_token = _GOLD_GROQ_RETRY_SPENT.set(False)
        try:
            with (
                gold_over_capacity_cooldown_scope(),
                _scoped_gold_attempt_budget(),
                current(),
            ):
                yield
        finally:
            _GOLD_GROQ_RETRY_SPENT.reset(retry_token)
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
        "Gold Vision Priority Admission V1 installed: persisted Long+Short Gold admission cushion; "
        "one run-scoped provider-directed Groq retry; Gold-only physical Vision cap=6; "
        "semantic/quality/security gates unchanged"
    )

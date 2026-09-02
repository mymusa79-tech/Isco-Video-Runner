from __future__ import annotations

from isco_video_agent.ai_budget import get_active_budget_task

from scripts import provider_capacity_hardening as capacity
from scripts import run125_capacity_routing_closure as run125
from scripts import text_audit_provider_mesh as mesh


# Run #167 exposed a capability-ownership leak rather than a real provider outage.
# Run123 intentionally replaced the shared Groq pacing hook with Planning's
# failover-without-sleep policy. Mandatory text audits reuse the same capacity module,
# so they inherited that Planning-only behavior: Qwen had trustworthy TPM reset
# evidence in 0.08s, but the local admission rejection was surfaced as a provider
# failure even though no HTTP request had happened.
#
# Ownership rule:
# - Planning keeps its existing fast-failover policy unchanged.
# - Text Audit Mesh keeps its canonical model order and route width unchanged.
# - A busy audit route never waits when a later route that is already part of the
#   bounded audit mesh is immediately admissible or has a strictly nearer trustworthy
#   reset. This preserves Run #157's 120b -> Qwen failover semantics.
# - When the current route is the best bounded option, a mandatory audit may wait once
#   on trustworthy exact-model reset evidence <=60s before its existing single wire
#   attempt. This closes Run #167 without adding provider attempts.
# - Missing/untrusted/long reset evidence keeps the old failover-without-HTTP behavior.

_TEXT_AUDIT_TASK_KINDS = frozenset(
    {
        "FACTUALITY_AUDIT",
        "CONTENT_QUALITY_AUDIT",
        "TONE_QUALITY_AUDIT",
    }
)
_AUDIT_RESET_WAIT_MAX_SECONDS = 60.0
_AUDIT_RESET_GRACE_SECONDS = 1.5
_AUDIT_WAITED_TASK_IDS: set[str] = set()
_INSTALLED = False


def _active_text_audit() -> bool:
    active = get_active_budget_task()
    if active is None:
        return False
    return str(active.spec.kind) in _TEXT_AUDIT_TASK_KINDS


def _active_text_audit_task_id() -> str | None:
    active = get_active_budget_task()
    if active is None or str(active.spec.kind) not in _TEXT_AUDIT_TASK_KINDS:
        return None
    task_id = str(active.spec.task_id or "").strip()
    return task_id or None


def _busy_precheck_error(
    *,
    model: str,
    required: int,
    remaining: object,
    reset_in: float | None,
) -> RuntimeError:
    reset_text = "unknown" if reset_in is None else f"{reset_in:.2f}s"
    return RuntimeError(
        "GROQ_TPM_WINDOW_BUSY_PRECHECK "
        f"model={model} required_estimate={required} remaining={remaining} "
        f"reset_in={reset_text} max_wait={_AUDIT_RESET_WAIT_MAX_SECONDS:.2f}s "
        "action=failover_without_http"
    )


def _trusted_reset_in_seconds(model_name: str) -> float | None:
    state = capacity._model_state(model_name)
    reset_at_epoch = state.get("reset_at_epoch")
    if not isinstance(reset_at_epoch, (int, float)):
        return None
    return max(0.0, float(reset_at_epoch) - capacity.time.time())


def _later_bounded_route_is_better(
    *,
    model_name: str,
    required: int,
    current_reset_in: float,
) -> bool:
    """Return True only when the existing bounded mesh has a better later Groq route.

    OpenRouter-healthy audits intentionally own only one Groq route, so there is no
    Groq look-ahead in that topology. When OpenRouter is blocked the existing mesh owns
    up to two Groq routes. We inspect only the next *eligible* route in the canonical
    active pool and never reorder or widen that pool.
    """
    if not run125.openrouter_preflight_blocked():
        return False

    tail = tuple(str(model) for model in mesh._active_groq_pool_tail())
    try:
        current_index = tail.index(str(model_name))
    except ValueError:
        return False

    for later_model in tail[current_index + 1 :]:
        try:
            decision = capacity.groq_admission_decision(later_model, required)
        except Exception:
            # Missing capacity evidence must be transparent to the historical mesh.
            # Do not invent a preferred route from a failed local probe.
            continue

        action = str(decision.get("action") or "")
        if action in {"impossible", "unavailable"}:
            continue
        if action != "wait":
            return True

        later_reset = _trusted_reset_in_seconds(later_model)
        if later_reset is None:
            # The canonical mesh does not consider a waiting model eligible without a
            # trustworthy reset timestamp, so keep looking for the next eligible route.
            continue
        return later_reset < current_reset_in

    return False


def _audit_wait_pacing(
    request_capacity: dict,
    model_name: str = capacity._DEFAULT_GROQ_MODEL,
) -> float:
    """Own audit admission without changing route order or provider-attempt budgets."""
    required = int(request_capacity["estimated_request_tokens"])
    model = str(model_name or capacity._DEFAULT_GROQ_MODEL).strip() or capacity._DEFAULT_GROQ_MODEL
    decision = capacity.groq_admission_decision(model, required)
    action = str(decision.get("action") or "")

    if action == "impossible":
        marker = (
            "GROQ_ACTUAL_TPM_BELOW_REQUEST"
            if decision.get("reason") == "actual_limit_below_required"
            else "GROQ_TPM_CAPACITY_PREFLIGHT"
        )
        raise RuntimeError(
            f"{marker} model={model} required={required} limit={decision.get('actual_limit')}"
        )
    if action == "unavailable":
        raise RuntimeError(
            "GROQ_MODEL_CAPACITY_UNAVAILABLE "
            f"model={model} reason={decision.get('reason')}"
        )
    if action != "wait":
        # No capacity pressure: be exactly transparent to the old audit path.
        return 0.0

    reset_in = _trusted_reset_in_seconds(model)
    if reset_in is None:
        raise _busy_precheck_error(
            model=model,
            required=required,
            remaining=decision.get("remaining_tokens"),
            reset_in=None,
        )
    if reset_in > _AUDIT_RESET_WAIT_MAX_SECONDS:
        raise _busy_precheck_error(
            model=model,
            required=required,
            remaining=decision.get("remaining_tokens"),
            reset_in=reset_in,
        )

    # Preserve canonical order while still choosing the cheapest admission decision:
    # fail over from a 38.40s 120b reset to Run167's later 0.08s Qwen route, or from a
    # busy model to an immediately admissible later route. The later route itself then
    # owns the only bounded wait if it still needs one.
    if _later_bounded_route_is_better(
        model_name=model,
        required=required,
        current_reset_in=reset_in,
    ):
        raise _busy_precheck_error(
            model=model,
            required=required,
            remaining=decision.get("remaining_tokens"),
            reset_in=reset_in,
        )

    task_id = _active_text_audit_task_id()
    if task_id is not None and task_id in _AUDIT_WAITED_TASK_IDS:
        # One bounded admission wait per mandatory audit task. A later provider route
        # may still run if immediately admissible, but cannot create a second sleep.
        raise _busy_precheck_error(
            model=model,
            required=required,
            remaining=decision.get("remaining_tokens"),
            reset_in=reset_in,
        )

    wait_seconds = min(
        reset_in + _AUDIT_RESET_GRACE_SECONDS,
        _AUDIT_RESET_WAIT_MAX_SECONDS + _AUDIT_RESET_GRACE_SECONDS,
    )
    if task_id is not None:
        _AUDIT_WAITED_TASK_IDS.add(task_id)
    print(
        "Text Audit Groq admission wait: "
        f"model={model} required={required} remaining={decision.get('remaining_tokens')} "
        f"reset_in={reset_in:.2f}s wait={wait_seconds:.2f}s owner=text_audit"
    )
    capacity.time.sleep(wait_seconds)
    return wait_seconds


def install_text_audit_capacity_ownership() -> None:
    """Give mandatory text audits their own bounded admission policy on shared Groq."""
    global _INSTALLED
    if _INSTALLED:
        return

    planning_pacing = capacity._proactive_groq_pacing

    def capability_owned_pacing(
        request_capacity: dict,
        model_name: str = capacity._DEFAULT_GROQ_MODEL,
    ) -> float:
        if _active_text_audit():
            return _audit_wait_pacing(request_capacity, model_name=model_name)
        return planning_pacing(request_capacity, model_name=model_name)

    capability_owned_pacing._isco_text_audit_capacity_owner_v1 = True
    capability_owned_pacing._isco_planning_pacing = planning_pacing
    capacity._proactive_groq_pacing = capability_owned_pacing

    # Deliberately do NOT replace mesh._groq_route_models. Run #157 established that
    # model order/pool width belong to Text Audit Mesh itself. This owner only changes
    # what a selected busy route may do before its wire boundary.
    mesh._ISCO_TEXT_AUDIT_CAPACITY_OWNERSHIP_V1 = True
    _INSTALLED = True
    print(
        "Text Audit capacity ownership installed: "
        "planning=fast_failover_preserved audit=single_bounded_reset_wait<=60s "
        "groq_order=mesh_preserved prewire_wait=zero_provider_attempts"
    )

from __future__ import annotations

import re
import time
from contextlib import contextmanager
from dataclasses import replace
from typing import Iterator

import isco_video_agent.ai_budget as ai_budget
import isco_video_agent.final_critic as final_critic
import isco_video_agent.production_pipeline as production_pipeline
from isco_video_agent.ai_budget import AttemptOutcome, Capability, TaskSpec
from isco_video_agent.orchestrator import _ledger_authorize, _ledger_record
from isco_video_agent.providers.openrouter import json_text as openrouter_json_text
from isco_video_agent.text_audit_router import _classify_exception
from scripts import run123_budget_closure as run123
from scripts import run181_vision_mesh_closure as vision_mesh
from scripts.retry_after_policy import retry_delay_decision


_GOLD_RELEASE_TASK = "GOLD_FINAL_CRITIC_RELEASE_REVIEW"
_GOLD_OPENING_VISION_TASK = "GOLD_FINAL_CRITIC_OPENING_VISUAL"
_OPENROUTER_MODEL = "openrouter/free"
_FINAL_CRITIC_VISION_MAX_PROVIDER_ATTEMPTS = 4
_FINAL_CRITIC_TEXT_MAX_PROVIDER_ATTEMPTS = 2
_FINAL_CRITIC_TOTAL_PROVIDER_ATTEMPTS = (
    _FINAL_CRITIC_VISION_MAX_PROVIDER_ATTEMPTS + _FINAL_CRITIC_TEXT_MAX_PROVIDER_ATTEMPTS
)
_GEMINI_RETRY_AFTER_WAIT_BUDGET_SECONDS = 30.0

_RETRY_AFTER_PATTERNS = (
    re.compile(r"please\s+retry\s+in\s+([0-9]+(?:\.[0-9]+)?)\s*s", re.I),
    re.compile(r"retry[-_ ]?after[^0-9]{0,24}([0-9]+(?:\.[0-9]+)?)\s*s?", re.I),
    re.compile(r"retry[_ ]?delay[^0-9]{0,24}([0-9]+(?:\.[0-9]+)?)\s*s", re.I),
)


def _two_attempt_spec(spec: TaskSpec) -> TaskSpec:
    return TaskSpec(
        task_id=spec.task_id,
        kind=spec.kind,
        priority=spec.priority,
        capability=spec.capability,
        max_provider_attempts=2,
        schema_repair_allowed=False,
        local_fallback=False,
        semantic_block_is_final=True,
    )


def _provider_review(audit_fn, provider: str, *args, **kwargs):
    """Run the unchanged Final Critic while exposing only provider-call failures.

    final_critic.audit_final_release intentionally catches json_text exceptions and
    converts them to a fail-closed critic result. We keep that behavior untouched,
    but capture the underlying provider exception so Gold can distinguish a technical
    provider failure from a genuine semantic BLOCK and switch provider only for the
    former.
    """
    original_json_text = final_critic.json_text
    captured: dict[str, Exception] = {}

    if provider == "gemini":
        def routed_json_text(api_key: str, prompt: str, *, model: str):
            try:
                return original_json_text(api_key, prompt, model=model)
            except Exception as exc:
                captured["error"] = exc
                raise
    elif provider == "openrouter":
        def routed_json_text(_api_key: str, prompt: str, *, model: str):
            del model
            try:
                return openrouter_json_text(prompt, model=_OPENROUTER_MODEL)
            except Exception as exc:
                captured["error"] = exc
                raise
    else:
        raise ValueError(f"Unsupported Final Critic text provider: {provider}")

    final_critic.json_text = routed_json_text
    try:
        result = audit_fn(*args, **kwargs)
    finally:
        final_critic.json_text = original_json_text
    return result, captured.get("error")


def _release_review_with_fallback(
    original_call_status,
    ledger,
    spec: TaskSpec,
    provider: str,
    resolved_model: str,
    audit_fn,
    *args,
    **kwargs,
):
    if spec.task_id != _GOLD_RELEASE_TASK or spec.capability is not Capability.TEXT:
        return original_call_status(
            ledger, spec, provider, resolved_model, audit_fn, *args, **kwargs
        )

    # Gold release text review gets exactly one technical provider switch:
    # Gemini -> OpenRouter. A valid semantic BLOCK remains authoritative.
    fallback_spec = _two_attempt_spec(spec)
    last_result: dict | None = None
    for provider_name, model_name in (
        ("gemini", resolved_model),
        ("openrouter", _OPENROUTER_MODEL),
    ):
        _ledger_authorize(ledger, fallback_spec)
        result, provider_error = _provider_review(
            audit_fn, provider_name, *args, **kwargs
        )
        last_result = result
        if provider_error is not None:
            _ledger_record(
                ledger,
                fallback_spec.task_id,
                provider=provider_name,
                resolved_model=model_name,
                capability=Capability.TEXT,
                outcome=_classify_exception(provider_error),
            )
            # Only technical/provider failure is eligible for the single switch.
            if provider_name == "gemini":
                continue
            return result

        outcome = (
            AttemptOutcome.CONTENT_BLOCKED
            if result.get("status") == "block"
            else AttemptOutcome.SUCCESS
        )
        _ledger_record(
            ledger,
            fallback_spec.task_id,
            provider=provider_name,
            resolved_model=model_name,
            capability=Capability.TEXT,
            outcome=outcome,
        )
        # A valid semantic BLOCK is authoritative; never shop another provider.
        return result

    return last_result or {"status": "block"}


def _retry_after_hint_seconds(exc: BaseException) -> float | None:
    """Extract only explicit provider Retry-After evidence from a Gemini failure."""
    for attr in ("retry_after", "retry_after_seconds"):
        value = getattr(exc, attr, None)
        if value is None:
            continue
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            continue
        if seconds >= 0:
            return seconds

    text = str(exc)
    for pattern in _RETRY_AFTER_PATTERNS:
        match = pattern.search(text)
        if match:
            try:
                return float(match.group(1))
            except (TypeError, ValueError):
                return None
    return None


def _gemini_with_retry_after_once(
    ledger,
    spec: TaskSpec,
    resolved_model: str,
    audit_fn,
):
    """Wrap one Gemini Vision wire call with at most one provider-directed retry.

    The shared Run181 router owns provider failover. This helper adds only the missing
    Final-Critic behavior: when Gemini itself supplies a short Retry-After on a
    rate/quota failure, account the failed wire attempt, wait the exact provider delay,
    authorize one second Gemini wire call, and return/raise that result to the shared
    router. No local retry is invented when the provider supplied no delay.
    """
    def call(*args, **kwargs):
        try:
            return audit_fn(*args, **kwargs)
        except Exception as first_error:
            code = vision_mesh.contract._classify_gemini_failure(first_error)
            detail = vision_mesh.contract.legacy._safe_exception_detail(first_error)
            hint = _retry_after_hint_seconds(first_error)
            if (
                code is not vision_mesh.contract.VisionErrorCode.PROVIDER_TRANSIENT
                or not vision_mesh._quota_or_rate_failure(detail)
                or hint is None
            ):
                raise

            decision = retry_delay_decision(
                provider_hint=hint,
                calculated_delay_seconds=0.0,
                wait_budget_seconds=_GEMINI_RETRY_AFTER_WAIT_BUDGET_SECONDS,
            )
            if decision.action != "retry" or decision.delay_seconds is None:
                print(
                    "Gold Final Critic Vision: Gemini Retry-After exceeds bounded wait; "
                    f"hint={decision.provider_hint_seconds} budget={decision.wait_budget_seconds}; "
                    "failing over through shared Vision mesh"
                )
                raise

            # The shared router already authorized the first Gemini wire attempt before
            # entering this wrapper. Record that failed wire now so the ledger remains
            # truthful, then authorize the one retry. The router records the retry's
            # terminal outcome when this wrapper returns or raises.
            vision_mesh.contract._record(
                ledger,
                spec,
                provider="gemini",
                requested_model=resolved_model,
                resolved_model=resolved_model,
                outcome=vision_mesh.contract._attempt_outcome(first_error),
                detail=detail,
            )
            vision_mesh.contract._authorize(ledger, spec)
            print(
                "Gold Final Critic Vision: honoring Gemini Retry-After once; "
                f"delay_seconds={decision.delay_seconds:.3f}"
            )
            time.sleep(decision.delay_seconds)
            return audit_fn(*args, **kwargs)

    return call


def _opening_vision_with_mesh(
    original_call_status,
    ledger,
    spec: TaskSpec,
    provider: str,
    resolved_model: str,
    audit_fn,
    *args,
    **kwargs,
):
    if (
        spec.task_id != _GOLD_OPENING_VISION_TASK
        or spec.capability is not Capability.VISION
        or provider != "gemini"
    ):
        return original_call_status(
            ledger, spec, provider, resolved_model, audit_fn, *args, **kwargs
        )

    # Adapt only the call contract. Provider choice, health aggregation, schema,
    # Engine visual normalizer, semantic-BLOCK finality, Groq/OpenRouter fallback and
    # circuit behavior remain owned by Run181's shared Long+Short Vision router.
    routed_spec = replace(
        spec,
        kind="VISUAL_AUDIT",
        max_provider_attempts=max(
            _FINAL_CRITIC_VISION_MAX_PROVIDER_ATTEMPTS,
            spec.max_provider_attempts,
        ),
        semantic_block_is_final=True,
    )
    retrying_gemini = _gemini_with_retry_after_once(
        ledger,
        routed_spec,
        resolved_model,
        audit_fn,
    )
    return vision_mesh._route_visual_audit_v3(
        ledger,
        routed_spec,
        provider,
        resolved_model,
        retrying_gemini,
        *args,
        **kwargs,
    )


def _ensure_final_critic_provider_budget() -> None:
    """Expand only the release reserve required by the newly reachable provider path.

    Run123 budgeted three Final-Critic attempts: opening Vision=1 plus text=2. The
    Run222 closure makes opening Vision truthfully bounded at four physical provider
    attempts (Gemini + one explicit Retry-After retry + Groq + OpenRouter), so the
    enforcing release path needs six total slots. Keep the old P2 ceiling unchanged by
    increasing the run hard cap and the P1+P0 reserve by the same delta.
    """
    baseline = int(getattr(run123, "_FINAL_CRITIC_PROVIDER_ATTEMPTS", 3))
    delta = max(0, _FINAL_CRITIC_TOTAL_PROVIDER_ATTEMPTS - baseline)
    for fmt, base_cap in run123.RUN123_PROVIDER_ATTEMPT_HARD_CAP.items():
        desired_cap = int(base_cap) + delta
        ai_budget.PROVIDER_ATTEMPT_HARD_CAP[fmt] = max(
            int(ai_budget.PROVIDER_ATTEMPT_HARD_CAP.get(fmt, 0)),
            desired_cap,
        )
        current_reserve = int(ai_budget.P1_AND_P0_RESERVED_BUFFER.get(fmt, 0))
        ai_budget.P1_AND_P0_RESERVED_BUFFER[fmt] = max(
            current_reserve,
            _FINAL_CRITIC_TOTAL_PROVIDER_ATTEMPTS,
        )


@contextmanager
def gold_final_critic_text_fallback() -> Iterator[None]:
    """Bind enforced Gold Final Critic to the shared provider meshes.

    The public context-manager name is retained for compatibility. Text keeps its
    existing Gemini->OpenRouter technical fallback. Opening Vision now enters the
    existing Gemini->Groq->OpenRouter Vision mesh, with one bounded provider-directed
    Gemini Retry-After retry. Semantic BLOCK remains final on both modalities.
    """
    _ensure_final_critic_provider_budget()
    original_call_status = production_pipeline._ledger_call_status

    def routed_call_status(ledger, spec, provider, resolved_model, fn, *args, **kwargs):
        if (
            getattr(spec, "task_id", "") == _GOLD_OPENING_VISION_TASK
            and getattr(spec, "capability", None) is Capability.VISION
        ):
            return _opening_vision_with_mesh(
                original_call_status,
                ledger,
                spec,
                provider,
                resolved_model,
                fn,
                *args,
                **kwargs,
            )
        return _release_review_with_fallback(
            original_call_status,
            ledger,
            spec,
            provider,
            resolved_model,
            fn,
            *args,
            **kwargs,
        )

    production_pipeline._ledger_call_status = routed_call_status
    try:
        yield
    finally:
        production_pipeline._ledger_call_status = original_call_status

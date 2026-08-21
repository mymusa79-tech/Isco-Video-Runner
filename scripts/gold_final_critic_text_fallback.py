from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import isco_video_agent.final_critic as final_critic
import isco_video_agent.production_pipeline as production_pipeline
from isco_video_agent.ai_budget import AttemptOutcome, Capability, TaskSpec
from isco_video_agent.orchestrator import _ledger_authorize, _ledger_record
from isco_video_agent.providers.openrouter import json_text as openrouter_json_text
from isco_video_agent.text_audit_router import _classify_exception


_GOLD_RELEASE_TASK = "GOLD_FINAL_CRITIC_RELEASE_REVIEW"
_OPENROUTER_MODEL = "openrouter/free"


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
    converts them to a fail-closed critic result.  We keep that behavior untouched,
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
    # Gemini -> OpenRouter.  Vision remains on the original direct Gemini path.
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


@contextmanager
def gold_final_critic_text_fallback() -> Iterator[None]:
    """Temporarily add a Gold-only text fallback without touching core critic logic."""
    original_call_status = production_pipeline._ledger_call_status

    def routed_call_status(ledger, spec, provider, resolved_model, fn, *args, **kwargs):
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

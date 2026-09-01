from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable

import isco_video_agent.content_quality as content_quality
import isco_video_agent.factuality as factuality
import isco_video_agent.orchestrator as orchestrator
import isco_video_agent.text_audit_router as engine_audit_router
import isco_video_agent.tone_quality as tone_quality
from isco_video_agent.ai_budget import get_active_budget_task

from scripts import provider_capacity_hardening as capacity
from scripts import run125_capacity_routing_closure as run125
from scripts import task_level_planner_router as planner_router


# Run 156 proved that provider-level fallback alone was not enough. Planning can move
# the Groq cursor from 20b -> 120b, while the old audit adapter then pinned every later
# factuality/content/tone audit to that single active model. If OpenRouter is preflight
# blocked and Gemini is quota-limited, a model-specific Groq failure can therefore
# exhaust a mandatory audit even though another Groq model is still available.
#
# This V2 keeps the same logical-task budget:
# - OpenRouter healthy: Gemini + one eligible Groq model + OpenRouter = max 3 attempts.
# - OpenRouter blocked: Gemini + up to two eligible Groq models = max 3 attempts.
#
# Each Groq model remains a distinct routing/circuit key so one model's rate limit or
# transport failure cannot poison another model. Ledger accounting normalizes those
# model-specific route labels back to provider="groq" while preserving resolved_model.
# No provider-internal retry, quality rule, quota, or hard gate is changed.

_INSTALLED = False
_GROQ_COMPLETION_RESERVE_TOKENS = 2_200
_GROQ_ROUTE_PREFIX = "groq:"
_AUDIT_ROUTE_TELEMETRY: list[dict] = []

_REQUIRED_ARRAYS_BY_TASK_KIND = {
    "FACTUALITY_AUDIT": (
        "unsupported_claims",
        "professional_advice_flags",
        "expert_persona_flags",
        "notes",
    ),
    "CONTENT_QUALITY_AUDIT": ("duplicate_groups",),
    "TONE_QUALITY_AUDIT": (
        "preachiness_flags",
        "cultural_dignity_flags",
        "naturalness_flags",
        "narrative_format_flags",
        "unverified_religious_quote_flags",
        "notes",
    ),
}


class TextAuditUnavailableError(RuntimeError):
    """A mandatory independent audit could not obtain a valid provider verdict."""


def _groq_secret_available() -> bool:
    direct = str(os.environ.get("GROQ_API_KEY") or "").strip()
    if direct:
        return True
    file_name = str(os.environ.get("GROQ_API_KEY_FILE") or "").strip()
    return bool(file_name and Path(file_name).is_file())


def _groq_token() -> str:
    direct = str(os.environ.get("GROQ_API_KEY") or "").strip()
    if direct:
        return direct
    return planner_router._read_secret_file("GROQ_API_KEY_FILE")


def _active_groq_pool_tail() -> tuple[str, ...]:
    """Return the current model and only models not already abandoned before it."""
    pool = tuple(getattr(run125, "_GROQ_MODEL_POOL", ()))
    if not pool:
        return (str(run125._active_groq_model()),)
    active = str(run125._active_groq_model())
    try:
        index = pool.index(active)
    except ValueError:
        return (active,)
    return tuple(str(model) for model in pool[index:])


def _groq_request_capacity(prompt: str, model_name: str) -> tuple[dict, dict]:
    estimate = capacity.groq_capacity_estimate(
        prompt,
        model_name=model_name,
        reserved_completion_tokens=_GROQ_COMPLETION_RESERVE_TOKENS,
        contract_name="text_audit_json",
    )
    decision = capacity.groq_admission_decision(
        model_name, int(estimate["estimated_request_tokens"])
    )
    return estimate, decision


def _groq_model_route_eligible(prompt: str, model_name: str) -> bool:
    """Admission-only eligibility. No provider call and no budget attempt happens here."""
    if not _groq_secret_available():
        return False
    try:
        _estimate, decision = _groq_request_capacity(prompt, model_name)
    except Exception:
        return False
    action = str(decision.get("action") or "")
    if action in {"impossible", "unavailable"}:
        return False
    if action != "wait":
        return True
    state = capacity._model_state(model_name)
    return isinstance(state.get("reset_at_epoch"), (int, float))


def _groq_audit_json(prompt: str, *, model_name: str) -> dict:
    """Exactly one Groq HTTP attempt for exactly one explicit model."""
    request_capacity, decision = _groq_request_capacity(prompt, model_name)
    required = int(request_capacity["estimated_request_tokens"])
    if decision.get("action") == "impossible":
        raise RuntimeError(
            "GROQ_AUDIT_TPM_HEADROOM_UNAVAILABLE "
            f"model={model_name} required={required} limit={decision.get('actual_limit')}"
        )
    if decision.get("action") == "unavailable":
        raise RuntimeError(
            "GROQ_AUDIT_MODEL_UNAVAILABLE "
            f"model={model_name} reason={decision.get('reason')}"
        )

    capacity._proactive_groq_pacing(request_capacity, model_name=model_name)
    token = _groq_token()

    payload = {
        "model": model_name,
        "messages": [
            {
                "role": "user",
                "content": prompt + "\nReturn ONLY one complete valid JSON object. No markdown.",
            }
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
        "max_completion_tokens": _GROQ_COMPLETION_RESERVE_TOKENS,
    }
    if model_name.startswith("qwen/"):
        payload["reasoning_effort"] = "none"
        payload["include_reasoning"] = False
    else:
        # Text audits are classification/editing judgments, not long-form generation.
        # Keep reasoning bounded so the response budget remains available for JSON.
        payload["reasoning_effort"] = "low"
        payload["include_reasoning"] = False

    response = planner_router.requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        json=payload,
        timeout=90,
    )
    capacity.observe_groq_response(
        response,
        model_name,
        required_tokens=required,
    )
    if not response.ok:
        raise RuntimeError(
            capacity._safe_groq_error(
                response,
                model_name=model_name,
                required_tokens=required,
            )
        )

    body = response.json()
    choices = body.get("choices") or []
    if not choices:
        raise RuntimeError("Groq text audit returned no choices")
    choice = choices[0]
    finish = str(choice.get("finish_reason") or "").strip().lower()
    if finish in {"length", "max_tokens"}:
        raise RuntimeError("GROQ_AUDIT_PREMATURE_RESPONSE finish_reason=length")
    content = str((choice.get("message") or {}).get("content") or "")
    if not content.strip():
        raise RuntimeError("GROQ_AUDIT_EMPTY_OUTPUT")
    return planner_router._parse_json(content)


def _active_required_arrays() -> tuple[str, ...] | None:
    active = get_active_budget_task()
    if active is None:
        return None
    return _REQUIRED_ARRAYS_BY_TASK_KIND.get(str(active.spec.kind))


def _contract_validated(call: Callable[[str], dict]) -> Callable[[str], dict]:
    """Make schema-invalid provider replies retryable before Approval Shopping decides."""
    def validated(prompt: str) -> dict:
        result = call(prompt)
        required = _active_required_arrays()
        if required is None:
            return result
        try:
            engine_audit_router.validate_audit_payload(result, required_arrays=required)
        except Exception as exc:
            raise RuntimeError(f"invalid JSON text audit contract: {exc}") from exc
        return result

    return validated


def _groq_route_label(model_name: str) -> str:
    return _GROQ_ROUTE_PREFIX + model_name


def _groq_route_models(prompt: str, *, openrouter_blocked: bool) -> list[str]:
    """Select bounded model candidates without consuming provider attempts."""
    max_models = 2 if openrouter_blocked else 1
    selected: list[str] = []
    for model_name in _active_groq_pool_tail():
        if _groq_model_route_eligible(prompt, model_name):
            selected.append(model_name)
        if len(selected) >= max_models:
            break
    return selected


def _route_attempt_dict(attempt) -> dict:
    return {
        "provider": str(attempt.provider),
        "outcome": attempt.outcome.value,
        "detail": attempt.detail,
    }


def _record_route_telemetry(route_result) -> None:
    active = get_active_budget_task()
    _AUDIT_ROUTE_TELEMETRY.append(
        {
            "task_id": active.spec.task_id if active is not None else None,
            "task_kind": active.spec.kind if active is not None else None,
            "winner": route_result.provider,
            "exhausted": bool(route_result.exhausted),
            "attempts": [_route_attempt_dict(attempt) for attempt in route_result.attempts],
        }
    )


def attach_text_audit_telemetry(telemetry_path: Path) -> Path:
    """Attach safe audit-route evidence to the existing durable planning telemetry."""
    path = Path(telemetry_path)
    if not path.is_file():
        return path
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("planning telemetry must be a JSON object")
    data["text_audit_provider_mesh"] = {
        "schema_version": 2,
        "routes": list(_AUDIT_ROUTE_TELEMETRY),
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _mesh_route(
    providers: list[tuple[str, Callable[[str], dict]]],
    prompt: str,
    *,
    cooldown: set[str] | None = None,
):
    """Gemini -> bounded Groq model candidates -> OpenRouter."""
    names = [str(name) for name, _call in providers]
    if "gemini" not in names or "openrouter" not in names:
        result = engine_audit_router.route_text_audit(providers, prompt, cooldown=cooldown)
        _record_route_telemetry(result)
        return result

    openrouter_blocked = run125.openrouter_preflight_blocked()
    routed: list[tuple[str, Callable[[str], dict]]] = []

    for name, call in providers:
        if name == "gemini":
            routed.append((name, _contract_validated(call)))
            for model_name in _groq_route_models(
                prompt,
                openrouter_blocked=openrouter_blocked,
            ):
                def groq_call(p: str, model: str = model_name) -> dict:
                    return _groq_audit_json(p, model_name=model)

                routed.append(
                    (_groq_route_label(model_name), _contract_validated(groq_call))
                )
            continue

        if name == "openrouter":
            if openrouter_blocked:
                continue
            routed.append((name, _contract_validated(call)))
            continue

        routed.append((name, _contract_validated(call)))

    result = engine_audit_router.route_text_audit(routed, prompt, cooldown=cooldown)
    _record_route_telemetry(result)
    return result


def _audit_unavailable_dimension(name: str, audit: object) -> bool:
    if not isinstance(audit, dict):
        return False
    validation = str(audit.get("validation") or "").strip().lower()
    if validation in {"providers_exhausted", "malformed"}:
        return True
    error_message = str(audit.get("error_message") or "").lower()
    if "all text-audit providers exhausted" in error_message:
        return True

    if name == "factuality":
        unsupported = audit.get("unsupported_claims")
        if unsupported == ["Factuality audit could not be completed safely"]:
            return True
    return False


def _install_unavailable_before_repair_guard() -> None:
    if getattr(orchestrator, "_ISCO_TEXT_AUDIT_UNAVAILABLE_GUARD_V1", False):
        return
    original = orchestrator.build_dossier

    def guarded_build_dossier(*args, **kwargs):
        unavailable: list[str] = []
        mapping = {
            "factuality": kwargs.get("factuality_audit"),
            "semantic_repetition": kwargs.get("semantic_repetition_audit"),
            "tone": kwargs.get("tone_audit"),
        }
        for name, audit in mapping.items():
            if _audit_unavailable_dimension(name, audit):
                unavailable.append(name)
        if unavailable:
            raise TextAuditUnavailableError(
                "TEXT_AUDIT_UNAVAILABLE dimensions="
                + ",".join(unavailable)
                + " action=fail_closed_without_content_repair"
            )
        return original(*args, **kwargs)

    guarded_build_dossier._isco_text_audit_unavailable_guard_v1 = True
    guarded_build_dossier._isco_original = original
    orchestrator.build_dossier = guarded_build_dossier
    orchestrator._ISCO_TEXT_AUDIT_UNAVAILABLE_GUARD_V1 = True


def _install_three_attempt_task_budget() -> None:
    if getattr(orchestrator, "_ISCO_TEXT_AUDIT_THREE_PROVIDER_BUDGET_V1", False):
        return
    original = orchestrator._audit_spec

    def three_attempt_spec(*args, **kwargs):
        spec = original(*args, **kwargs)
        spec.max_provider_attempts = max(3, int(spec.max_provider_attempts))
        return spec

    three_attempt_spec._isco_text_audit_three_provider_budget_v1 = True
    three_attempt_spec._isco_original = original
    orchestrator._audit_spec = three_attempt_spec
    orchestrator._ISCO_TEXT_AUDIT_THREE_PROVIDER_BUDGET_V1 = True


def _install_model_aware_ledger_recording() -> None:
    """Normalize model-specific audit route keys back to provider=groq in BudgetLedger."""
    if getattr(engine_audit_router, "_ISCO_AUDIT_GROQ_MODEL_LEDGER_V1", False):
        return
    original = engine_audit_router._record_wire_attempt

    def record(
        provider: str,
        outcome,
        *,
        duration_seconds: float,
        detail: str | None = None,
    ) -> None:
        if not str(provider).startswith(_GROQ_ROUTE_PREFIX):
            return original(
                provider,
                outcome,
                duration_seconds=duration_seconds,
                detail=detail,
            )
        active = get_active_budget_task()
        if active is None:
            return
        model_name = str(provider)[len(_GROQ_ROUTE_PREFIX):]
        active.ledger.record_attempt(
            active.spec.task_id,
            provider="groq",
            requested_model=active.requested_model,
            resolved_model=model_name,
            capability=active.spec.capability,
            outcome=outcome,
            duration_seconds=duration_seconds,
            detail=detail,
        )

    record._isco_audit_groq_model_ledger_v1 = True
    record._isco_original = original
    engine_audit_router._record_wire_attempt = record
    engine_audit_router._ISCO_AUDIT_GROQ_MODEL_LEDGER_V1 = True


def _install_telemetry_attachment() -> None:
    """Patch live production entrypoints so failure telemetry keeps audit attempt detail."""
    from scripts.runtime_reliability import production_entrypoint_modules

    for production in production_entrypoint_modules():
        current = getattr(production, "write_planning_telemetry", None)
        if current is None or getattr(current, "_isco_text_audit_telemetry_v2", False):
            continue

        def make_wrapper(original):
            def wrapped(out_dir: Path):
                path = original(out_dir)
                return attach_text_audit_telemetry(path)

            wrapped._isco_text_audit_telemetry_v2 = True
            wrapped._isco_original = original
            return wrapped

        setattr(production, "write_planning_telemetry", make_wrapper(current))


def install_text_audit_provider_mesh() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    factuality.route_text_audit = _mesh_route
    content_quality.route_text_audit = _mesh_route
    tone_quality.route_text_audit = _mesh_route
    _install_model_aware_ledger_recording()
    _install_three_attempt_task_budget()
    _install_unavailable_before_repair_guard()
    _install_telemetry_attachment()

    _INSTALLED = True
    print(
        "Text Audit Provider Mesh V2 installed: "
        "route=gemini->groq_model_pool->openrouter "
        "max_attempts_per_audit=3 "
        "openrouter_blocked_allows_two_groq_models=true "
        "audit_contract_validation=task_kind_bound "
        "semantic_block_final=true technical_exhaustion_repair=false "
        "audit_attempt_detail_durable=true"
    )

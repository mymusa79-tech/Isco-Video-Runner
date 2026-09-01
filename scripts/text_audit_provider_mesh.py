from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

import isco_video_agent.content_quality as content_quality
import isco_video_agent.factuality as factuality
import isco_video_agent.orchestrator as orchestrator
import isco_video_agent.text_audit_router as engine_audit_router
import isco_video_agent.tone_quality as tone_quality

from scripts import provider_capacity_hardening as capacity
from scripts import run125_capacity_routing_closure as run125
from scripts import task_level_planner_router as planner_router


# Run 155 reached post-planning text QA for the first time after the native Short
# capacity closure. Planning had a healthy Groq path, but factuality/content/tone still
# used the historical Engine-only Gemini -> OpenRouter pair. When both were unavailable,
# the Engine converted provider exhaustion into semantic status=block and RepairDossier
# rewrote the script as if provider availability were a content defect.
#
# This adapter changes transport/availability semantics only:
# - one attempt per provider in Gemini -> Groq -> OpenRouter order;
# - Groq reuses the current model-scoped capacity/pacing evidence without double-counting
#   BudgetLedger (Engine's text_audit_router remains the sole attempt-accounting owner);
# - OpenRouter is omitted when the already-run provider preflight marked it blocked;
# - a real semantic status=block remains final (Approval Shopping is unchanged);
# - technical audit exhaustion fails closed BEFORE RepairDossier, so no pointless rewrite
#   can masquerade as a quality repair.
# No quality threshold, factuality rule, provider quota, or provider-internal retry is changed.

_INSTALLED = False
_GROQ_PROVIDER_NAME = "groq"
_GROQ_COMPLETION_RESERVE_TOKENS = 2_200


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


def _groq_audit_json(prompt: str) -> dict:
    """One Groq wire attempt, with the existing model-aware capacity contract.

    Budget accounting deliberately stays outside this function: Engine
    text_audit_router._call_provider() authorizes and records this exact wire call.
    Calling planner_router._groq_call() here would double-authorize the same attempt.
    """
    model_name = run125._active_groq_model()
    request_capacity = capacity.groq_capacity_estimate(
        prompt,
        model_name=model_name,
        reserved_completion_tokens=_GROQ_COMPLETION_RESERVE_TOKENS,
        contract_name="text_audit_json",
    )
    required = int(request_capacity["estimated_request_tokens"])
    decision = capacity.groq_admission_decision(model_name, required)
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

    # Existing capacity owner waits only from provider-observed reset evidence. It
    # never invents a delay and never waits when the actual TPM ceiling is too small.
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


def _mesh_route(
    providers: list[tuple[str, Callable[[str], dict]]],
    prompt: str,
    *,
    cooldown: set[str] | None = None,
):
    """Insert Groq between the Engine's existing Gemini and OpenRouter audit calls."""
    names = [str(name) for name, _call in providers]
    if "gemini" not in names or "openrouter" not in names:
        # Preserve standalone/custom Engine callers exactly; this adapter owns only the
        # production two-provider audit topology it was built to extend.
        return engine_audit_router.route_text_audit(providers, prompt, cooldown=cooldown)

    routed: list[tuple[str, Callable[[str], dict]]] = []
    groq_present = "groq" in names
    for name, call in providers:
        if name == "gemini":
            routed.append((name, call))
            if not groq_present and _groq_secret_available():
                routed.append((_GROQ_PROVIDER_NAME, _groq_audit_json))
            continue
        if name == "openrouter" and run125.openrouter_preflight_blocked():
            # A preflight-blocked provider is not a fallback. No wire call, no budget
            # attempt, and no fake semantic verdict are created.
            continue
        routed.append((name, call))

    return engine_audit_router.route_text_audit(routed, prompt, cooldown=cooldown)


def _audit_unavailable_dimension(name: str, audit: object) -> bool:
    if not isinstance(audit, dict):
        return False
    validation = str(audit.get("validation") or "").strip().lower()
    if validation in {"providers_exhausted", "malformed"}:
        return True
    error_message = str(audit.get("error_message") or "").lower()
    if "all text-audit providers exhausted" in error_message:
        return True

    # factuality keeps diagnostics in a separate sidecar for compatibility, so its
    # historical fail-closed result does not carry `validation`. Detect only its exact
    # technical sentinel; a real provider-authored unsupported claim never equals it.
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


def _install_three_provider_task_budget() -> None:
    if getattr(orchestrator, "_ISCO_TEXT_AUDIT_THREE_PROVIDER_BUDGET_V1", False):
        return
    original = orchestrator._audit_spec

    def three_provider_spec(*args, **kwargs):
        spec = original(*args, **kwargs)
        # Three independent providers, still exactly one wire attempt per provider.
        # This is a fallback-width change, not a provider retry increase.
        spec.max_provider_attempts = max(3, int(spec.max_provider_attempts))
        return spec

    three_provider_spec._isco_text_audit_three_provider_budget_v1 = True
    three_provider_spec._isco_original = original
    orchestrator._audit_spec = three_provider_spec
    orchestrator._ISCO_TEXT_AUDIT_THREE_PROVIDER_BUDGET_V1 = True


def install_text_audit_provider_mesh() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    # These Engine modules imported route_text_audit directly, so patch their bound
    # call sites explicitly. The underlying Engine router remains the sole owner of
    # cooldown, Approval Shopping, authorization, and attempt recording.
    factuality.route_text_audit = _mesh_route
    content_quality.route_text_audit = _mesh_route
    tone_quality.route_text_audit = _mesh_route
    _install_three_provider_task_budget()
    _install_unavailable_before_repair_guard()

    _INSTALLED = True
    print(
        "Text Audit Provider Mesh V1 installed: "
        "route=gemini->groq->openrouter one_attempt_each "
        "openrouter_preflight_respected=true "
        "semantic_block_final=true technical_exhaustion_repair=false"
    )

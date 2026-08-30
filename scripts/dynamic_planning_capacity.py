from __future__ import annotations

import json
import os
from pathlib import Path

from scripts import planning_batch_hardening as batching
from scripts import planning_stage_contract as stage_contract
from scripts import provider_capacity_hardening as capacity
from scripts import run125_capacity_routing_closure as run125
from scripts import task_level_planner_router as router

# Must match the production pool enforced by run125_cache_prefix_contract.py. Qwen is
# intentionally excluded there because it is preview-only and therefore must not make a
# capacity gate report a path that canonical production will never actually use.
_PRODUCTION_GROQ_MODELS = (
    "openai/gpt-oss-20b",
    "openai/gpt-oss-120b",
)
_FIRST_WRITER_GATE_SEEN = False
_INSTALLED = False


def _provider_preflight_path() -> Path | None:
    explicit = str(os.environ.get("ISCO_PROVIDER_PREFLIGHT_PATH") or "").strip()
    if explicit:
        return Path(explicit)
    temp = str(os.environ.get("RUNNER_TEMP") or "").strip()
    return Path(temp) / "provider-preflight.json" if temp else None


def _provider_statuses(path: Path | None = None) -> dict[str, str]:
    target = path or _provider_preflight_path()
    if target is None or not target.is_file():
        return {}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    result: dict[str, str] = {}
    for check in payload.get("checks") or []:
        if not isinstance(check, dict):
            continue
        provider = str(check.get("provider") or "").strip().lower()
        status = str(check.get("status") or "").strip().lower()
        if provider:
            result[provider] = status
    return result


def viable_planning_providers(required_tokens: int, *, preflight_path: Path | None = None) -> list[str]:
    """Return providers that are not known incapable of this P0 planning request.

    Gemini/OpenRouter have no local TPM authority in this stack, so a passing provider
    readiness check keeps them viable. Groq is stricter: each production model uses
    provider-learned capacity, with 8000 only as its pre-contact bootstrap assumption.
    """
    statuses = _provider_statuses(preflight_path)
    viable: list[str] = []

    if statuses.get("gemini") == "pass":
        viable.append("gemini")

    groq_status = statuses.get("groq")
    # Local/unit contexts often do not materialize provider-preflight.json; only Groq's
    # deterministic bootstrap admission is used there so old split tests remain honest.
    if groq_status == "pass" or not statuses:
        for model in _PRODUCTION_GROQ_MODELS:
            decision = capacity.groq_admission_decision(model, required_tokens)
            if decision["action"] in {"admit", "unknown", "wait"}:
                viable.append(f"groq:{model}")
                break

    if statuses.get("openrouter") == "pass":
        viable.append("openrouter")
    return viable


def require_viable_planning_capacity(
    required_tokens: int,
    *,
    phase: str,
    preflight_path: Path | None = None,
) -> list[str]:
    viable = viable_planning_providers(required_tokens, preflight_path=preflight_path)
    if not viable:
        raise RuntimeError(
            "NO_VIABLE_PLANNING_CAPACITY "
            f"phase={phase} required_tokens={int(required_tokens)}"
        )
    return viable


def effective_request_capacity(prompt: str) -> dict:
    routed = router.with_channel_persona(router._enrich_dialogue_prompt(prompt))
    estimate = capacity.groq_capacity_estimate(routed)
    estimate["effective_routed_prompt"] = True
    return estimate


def certify_general_planning_envelope(required_tokens: int) -> list[str]:
    return require_viable_planning_capacity(
        required_tokens,
        phase="preproduction_general_envelope",
    )


def _dynamic_groq_model_call(prompt: str, model_name: str) -> dict:
    request_capacity = capacity.groq_capacity_estimate(prompt, model_name=model_name)
    decision = capacity.groq_admission_decision(
        model_name, request_capacity["estimated_request_tokens"]
    )
    if decision["action"] == "impossible":
        marker = (
            "GROQ_ACTUAL_TPM_BELOW_REQUEST"
            if decision["reason"] == "actual_limit_below_required"
            else "GROQ_TPM_CAPACITY_PREFLIGHT"
        )
        raise RuntimeError(
            f"{marker} model={model_name} required={request_capacity['estimated_request_tokens']} "
            f"limit={decision['actual_limit']}"
        )
    if decision["action"] == "unavailable":
        raise RuntimeError(
            "GROQ_MODEL_CAPACITY_UNAVAILABLE "
            f"model={model_name} reason={decision['reason']}"
        )

    capacity._proactive_groq_pacing(request_capacity, model_name=model_name)
    token = router._read_secret_file("GROQ_API_KEY_FILE")
    contract = router._structured_schema_for_prompt(prompt)
    contract_name = contract[0] if contract else "json_object"

    def do_request() -> dict:
        payload = {
            "model": model_name,
            "messages": [
                {
                    "role": "user",
                    "content": prompt + "\nReturn ONLY one complete valid JSON object. No markdown.",
                }
            ],
            "response_format": capacity._response_format_for_contract(contract),
            "temperature": 0.15,
            "max_completion_tokens": capacity.completion_token_budget(contract),
        }
        if contract_name == "editorial_outline" or contract_name in capacity._OUTPUT_HEAVY_CONTRACTS:
            payload["reasoning_effort"] = "low"
            payload["include_reasoning"] = False

        response = router.requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
            json=payload,
            timeout=90,
        )
        capacity.observe_groq_response(
            response,
            model_name,
            required_tokens=request_capacity["estimated_request_tokens"],
        )
        router._last_call_rate_limit_headers.update(
            router._extract_rate_limit_headers(response.headers)
        )
        if not response.ok:
            raise RuntimeError(
                capacity._safe_groq_error(
                    response,
                    model_name=model_name,
                    required_tokens=request_capacity["estimated_request_tokens"],
                )
            )
        body = response.json()
        choices = body.get("choices") or []
        if not choices:
            raise RuntimeError("Groq returned no choices")
        choice = choices[0]
        router._last_call_response_meta.update(router._extract_response_meta(body, choice))
        finish = str(choice.get("finish_reason") or "").strip().lower()
        if finish in {"length", "max_tokens"}:
            raise RuntimeError("GROQ_PREMATURE_RESPONSE finish_reason=length")
        content = str((choice.get("message") or {}).get("content") or "")
        if not content.strip():
            raise RuntimeError("GROQ_EMPTY_OUTPUT")
        return router._parse_json(content)

    return router._budgeted_provider_call("groq", model_name, do_request)


def install_dynamic_planning_capacity() -> None:
    """Bind model-scoped capacity authority after Run125 installs its model pool."""
    global _INSTALLED
    if _INSTALLED:
        return

    # Run125 remains the model-pool/failover owner; only the alternate model call gains
    # the learned capacity authority. run125_cache_prefix_contract later restricts its
    # pool to the same two production GPT-OSS models used by viability above.
    run125._groq_model_call = _dynamic_groq_model_call

    original_is_model_unavailable = run125._is_model_unavailable

    def request_incapable_model(error) -> bool:
        lower = str(error).lower()
        return (
            original_is_model_unavailable(error)
            or "groq_actual_tpm_below_request" in lower
            or "groq_model_capacity_unavailable" in lower
        )

    run125._is_model_unavailable = request_incapable_model

    # Replace Groq-only shard admission with provider-set viability. A weak Groq model
    # must not force Gemini/OpenRouter-capable Writer shards to split.
    def provider_set_admitted(prompt: str) -> tuple[bool, dict]:
        estimate = effective_request_capacity(prompt)
        viable = viable_planning_providers(estimate["estimated_request_tokens"])
        estimate["viable_providers"] = viable
        return bool(viable), estimate

    batching._capacity_admitted = provider_set_admitted
    original_shard = batching._call_capacity_aware_shard

    def exact_runtime_gate(
        api_key: str,
        model: str,
        ids: list[str],
        *,
        prompt_builder,
        label: str,
    ) -> dict[str, dict]:
        global _FIRST_WRITER_GATE_SEEN
        # This wrapper runs before the base batching function establishes its own
        # scope. Bind the exact request here as well so the early capacity gate cannot
        # fall back to prompt-derived schema/budget identity.
        with stage_contract.script_batch_scope(label, ids):
            prompt = prompt_builder(ids)
            estimate = effective_request_capacity(prompt)
            viable = viable_planning_providers(estimate["estimated_request_tokens"])

            if label == "writer" and not _FIRST_WRITER_GATE_SEEN:
                _FIRST_WRITER_GATE_SEEN = True
                print(
                    "Exact first Writer capacity gate: "
                    f"required={estimate['estimated_request_tokens']} "
                    f"viable={','.join(viable) if viable else 'none'}"
                )

            if not viable and len(ids) <= 1:
                raise RuntimeError(
                    "NO_VIABLE_PLANNING_CAPACITY "
                    f"phase=exact_runtime_{label} section={ids[0]} "
                    f"required_tokens={estimate['estimated_request_tokens']}"
                )
            return original_shard(
                api_key,
                model,
                ids,
                prompt_builder=prompt_builder,
                label=label,
            )

    batching._call_capacity_aware_shard = exact_runtime_gate
    _INSTALLED = True
    print(
        "Dynamic planning capacity installed: "
        "model_scoped_groq=true actual_limit_below_required_zero_sleep=true "
        "provider_set_shard_admission=true exact_first_writer_gate=true"
    )

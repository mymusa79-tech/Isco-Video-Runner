from __future__ import annotations

import math

from scripts import task_level_planner_router as router

# Groq's current Free-plan limit for openai/gpt-oss-20b is 8K combined TPM. The
# model context window is much larger, so context-size admission alone is not enough.
# Keep a local safety reserve for adapter/schema overhead and use a deliberately
# conservative empirical byte/token ratio calibrated against Arabic Run #117 prompts.
GROQ_FREE_TPM_LIMIT = 8_000
GROQ_TOKEN_SAFETY_RESERVE = 250
GROQ_ESTIMATED_UTF8_BYTES_PER_TOKEN = 4.25
MAX_RETRY_AFTER_SECONDS = 120.0

_COMPLETION_TOKEN_BUDGETS = {
    "editorial_outline": 2_400,
    # Long-form writing is now max four sections per call. 2.4K leaves ample room for
    # ~480 Arabic spoken words plus JSON while fitting Groq's 8K TPM envelope.
    "full_script": 2_400,
    "append_only_repair": 1_800,
    "section_repair": 1_400,
    "json_object": 2_200,
}


def _contract_name(prompt: str) -> str:
    contract = router._structured_schema_for_prompt(prompt)
    return contract[0] if contract else "json_object"


def completion_token_budget(contract) -> int:
    name = contract[0] if contract else "json_object"
    return _COMPLETION_TOKEN_BUDGETS.get(name, _COMPLETION_TOKEN_BUDGETS["json_object"])


def estimate_prompt_tokens(prompt: str) -> int:
    """Conservative local estimate used only for admission, never for billing."""
    prompt_bytes = len(prompt.encode("utf-8"))
    return max(1, math.ceil(prompt_bytes / GROQ_ESTIMATED_UTF8_BYTES_PER_TOKEN))


def groq_capacity_estimate(prompt: str) -> dict:
    contract = router._structured_schema_for_prompt(prompt)
    prompt_tokens = estimate_prompt_tokens(prompt)
    reserved_completion = completion_token_budget(contract)
    estimated_total = prompt_tokens + reserved_completion + GROQ_TOKEN_SAFETY_RESERVE
    return {
        "estimated_prompt_tokens": prompt_tokens,
        "reserved_completion_tokens": reserved_completion,
        "token_safety_reserve": GROQ_TOKEN_SAFETY_RESERVE,
        "estimated_request_tokens": estimated_total,
        "provider_tpm_limit": GROQ_FREE_TPM_LIMIT,
        "contract": contract[0] if contract else "json_object",
    }


def _hardened_openrouter_structured_request(prompt: str, contract: tuple[str, dict]) -> dict:
    """Structured OpenRouter call with bounded reasoning for output-heavy JSON tasks."""
    schema_name, schema = contract
    token = router._openrouter_key()

    def do_request() -> dict:
        request_payload = {
            "models": list(router._OPENROUTER_MODELS),
            "messages": [{"role": "user", "content": prompt + "\nReturn ONLY one complete valid JSON object. No markdown."}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            },
            "provider": {"allow_fallbacks": True, "require_parameters": True},
            "plugins": [{"id": "response-healing"}],
            "temperature": 0.3,
            "max_tokens": completion_token_budget(contract),
        }
        # Run #117's routed free model spent most of the output envelope on reasoning
        # and then ended with finish_reason=length. Keep useful reasoning for the
        # editorial blueprint, but minimize it for transport-heavy writing/repair.
        if schema_name == "editorial_outline":
            request_payload["reasoning"] = {"effort": "low", "exclude": True}
        else:
            request_payload["reasoning"] = {"effort": "minimal", "exclude": True}

        response = router.requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/mymusa79-tech/Isco-Video-Runner",
                "X-Title": "Isco Video Runner",
            },
            json=request_payload,
            timeout=120,
        )
        router._last_call_rate_limit_headers.update(router._extract_rate_limit_headers(response.headers))
        if not response.ok:
            raise RuntimeError(router._safe_api_error("OPENROUTER", response))
        body = response.json()
        choices = body.get("choices") or []
        if not choices:
            raise RuntimeError("OpenRouter returned no choices")
        choice = choices[0]
        router._last_call_response_meta.update(router._extract_response_meta(body, choice))
        finish = str(choice.get("finish_reason") or "").strip().lower()
        if finish in {"length", "max_tokens"}:
            raise RuntimeError("OPENROUTER_PREMATURE_RESPONSE finish_reason=length")
        content = str((choice.get("message") or {}).get("content") or "")
        if not content.strip():
            raise RuntimeError("OPENROUTER_EMPTY_OUTPUT")
        try:
            return router._parse_json(content)
        except RuntimeError as exc:
            if "invalid JSON" not in str(exc):
                raise
            raise router._OpenRouterMalformedJSON(content) from None

    return router._budgeted_provider_call("openrouter", "openrouter/free", do_request)


def install_provider_capacity_hardening() -> None:
    """Install Run #117 capacity safeguards before install_router() builds providers."""
    if getattr(router, "_ISCO_PROVIDER_CAPACITY_HARDENED", False):
        return

    original_groq_call = router._groq_call
    original_request_metadata = router._request_metadata

    def hardened_groq_call(prompt: str) -> dict:
        capacity = groq_capacity_estimate(prompt)
        if capacity["estimated_request_tokens"] > GROQ_FREE_TPM_LIMIT:
            raise RuntimeError(
                "GROQ_TPM_CAPACITY_PREFLIGHT "
                f"contract={capacity['contract']} "
                f"estimated_prompt_tokens={capacity['estimated_prompt_tokens']} "
                f"reserved_completion_tokens={capacity['reserved_completion_tokens']} "
                f"safety_tokens={capacity['token_safety_reserve']} "
                f"estimated_total={capacity['estimated_request_tokens']} "
                f"limit={GROQ_FREE_TPM_LIMIT}"
            )
        return original_groq_call(prompt)

    hardened_groq_call._isco_token_capacity_guard = True

    def hardened_request_metadata(prompt: str) -> dict:
        metadata = original_request_metadata(prompt)
        metadata.update(groq_capacity_estimate(prompt))
        return metadata

    router._completion_tokens_for_contract = completion_token_budget
    router._groq_call = hardened_groq_call
    router._request_metadata = hardened_request_metadata
    router._openrouter_structured_request = _hardened_openrouter_structured_request
    router.RETRY_AFTER_MAX_SECONDS = MAX_RETRY_AFTER_SECONDS
    router._ISCO_PROVIDER_CAPACITY_HARDENED = True
    print(
        "Provider capacity hardening installed: "
        f"groq_tpm={GROQ_FREE_TPM_LIMIT} retry_after_cap={MAX_RETRY_AFTER_SECONDS:g}s"
    )

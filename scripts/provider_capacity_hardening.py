from __future__ import annotations

import math
import re
import time

from scripts import task_level_planner_router as router

GROQ_FREE_TPM_LIMIT = 8_000
GROQ_TOKEN_SAFETY_RESERVE = 250
GROQ_ESTIMATED_UTF8_BYTES_PER_TOKEN = 4.25
MAX_RETRY_AFTER_SECONDS = 120.0
GROQ_RATE_RESET_SAFETY_SECONDS = 1.5

OPENROUTER_OUTPUT_HEAVY_MODEL = "openai/gpt-oss-20b:free"
OPENROUTER_OUTPUT_HEAVY_MODELS = (
    OPENROUTER_OUTPUT_HEAVY_MODEL,
    "nvidia/nemotron-3-super-120b-a12b:free",
    "openrouter/free",
)
_OUTPUT_HEAVY_CONTRACTS = frozenset({"full_script", "append_only_repair", "section_repair"})

_COMPLETION_TOKEN_BUDGETS = {
    "editorial_outline": 2_400,
    "full_script": 2_400,
    "append_only_repair": 1_800,
    "section_repair": 1_400,
    "json_object": 2_200,
}

_GROQ_RATE_STATE: dict[str, float | int | None] = {
    "remaining_tokens": None,
    "reset_at_monotonic": None,
}
_DURATION_PART_RE = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h)", flags=re.I)


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


def _response_format_for_contract(contract: tuple[str, dict] | None) -> dict:
    if contract is None:
        return {"type": "json_object"}
    schema_name, schema = contract
    if schema_name in _OUTPUT_HEAVY_CONTRACTS:
        return {"type": "json_object"}
    return {
        "type": "json_schema",
        "json_schema": {"name": schema_name, "strict": True, "schema": schema},
    }


def _header_value(headers, name: str):
    value = headers.get(name)
    if value is not None:
        return value
    lower_name = name.lower()
    for key, candidate in getattr(headers, "items", lambda: [])():
        if str(key).lower() == lower_name:
            return candidate
    return None


def _duration_header_seconds(value: object) -> float | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    try:
        direct = float(text)
    except ValueError:
        direct = None
    if direct is not None:
        return direct if direct >= 0 else None

    matches = list(_DURATION_PART_RE.finditer(text))
    if not matches or "".join(match.group(0) for match in matches) != text:
        return None
    factors = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}
    total = sum(float(match.group(1)) * factors[match.group(2).lower()] for match in matches)
    return total if total >= 0 else None


def _update_groq_rate_state(headers) -> None:
    remaining_raw = _header_value(headers, "x-ratelimit-remaining-tokens")
    reset_raw = _header_value(headers, "x-ratelimit-reset-tokens")
    try:
        remaining = max(0, int(float(str(remaining_raw).strip())))
    except (TypeError, ValueError):
        remaining = None
    reset_seconds = _duration_header_seconds(reset_raw)

    _GROQ_RATE_STATE["remaining_tokens"] = remaining
    _GROQ_RATE_STATE["reset_at_monotonic"] = (
        time.monotonic() + reset_seconds if reset_seconds is not None else None
    )


def _proactive_groq_pacing(capacity: dict) -> float:
    remaining = _GROQ_RATE_STATE.get("remaining_tokens")
    reset_at = _GROQ_RATE_STATE.get("reset_at_monotonic")
    required = int(capacity["estimated_request_tokens"])
    if not isinstance(remaining, int) or required <= remaining or not isinstance(reset_at, (int, float)):
        return 0.0

    until_reset = max(0.0, float(reset_at) - time.monotonic())
    delay = min(MAX_RETRY_AFTER_SECONDS, until_reset + GROQ_RATE_RESET_SAFETY_SECONDS)
    if delay <= 0:
        return 0.0
    print(
        "Groq proactive TPM pacing: "
        f"required_estimate={required} remaining={remaining} delay={delay:.2f}s"
    )
    time.sleep(delay)
    _GROQ_RATE_STATE["remaining_tokens"] = None
    _GROQ_RATE_STATE["reset_at_monotonic"] = None
    return delay


def _safe_groq_error(response) -> str:
    try:
        body = response.json()
    except Exception:
        body = None
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            code = str(err.get("code") or err.get("type") or "").strip().lower()
            message = str(err.get("message") or "").strip().lower()
            if code in {"json_validate_failed", "structured_generation_failed"} or "failed to validate json" in message:
                return f"GROQ_JSON_VALIDATE_FAILED status={int(response.status_code)} code={code or 'json_validate_failed'}"
    return router._safe_api_error("GROQ", response)


def _hardened_groq_call(prompt: str) -> dict:
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

    _proactive_groq_pacing(capacity)

    token = router._read_secret_file("GROQ_API_KEY_FILE")
    contract = router._structured_schema_for_prompt(prompt)
    contract_name = contract[0] if contract else "json_object"

    def do_request() -> dict:
        request_payload = {
            "model": "openai/gpt-oss-20b",
            "messages": [{"role": "user", "content": prompt + "\nReturn ONLY one complete valid JSON object. No markdown."}],
            "response_format": _response_format_for_contract(contract),
            "temperature": 0.15,
            "max_completion_tokens": completion_token_budget(contract),
        }
        if contract_name == "editorial_outline" or contract_name in _OUTPUT_HEAVY_CONTRACTS:
            request_payload["reasoning_effort"] = "low"
            request_payload["include_reasoning"] = False

        response = router.requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
            json=request_payload,
            timeout=90,
        )
        _update_groq_rate_state(response.headers)
        router._last_call_rate_limit_headers.update(router._extract_rate_limit_headers(response.headers))
        if not response.ok:
            raise RuntimeError(_safe_groq_error(response))
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

    return router._budgeted_provider_call("groq", "openai/gpt-oss-20b", do_request)


def _hardened_openrouter_structured_request(prompt: str, contract: tuple[str, dict]) -> dict:
    schema_name, _schema = contract
    token = router._openrouter_key()
    output_heavy = schema_name in _OUTPUT_HEAVY_CONTRACTS
    requested_model = "openrouter/free-model-fallbacks" if output_heavy else "openrouter/free"

    def do_request() -> dict:
        request_payload = {
            "models": (
                list(OPENROUTER_OUTPUT_HEAVY_MODELS)
                if output_heavy
                else list(router._OPENROUTER_MODELS)
            ),
            "messages": [{"role": "user", "content": prompt + "\nReturn ONLY one complete valid JSON object. No markdown."}],
            "response_format": _response_format_for_contract(contract),
            "provider": {"allow_fallbacks": True, "require_parameters": True},
            "plugins": [{"id": "response-healing"}],
            "temperature": 0.3,
            "max_tokens": completion_token_budget(contract),
        }
        # OpenRouter documents that reasoning tokens consume the output budget.  Run
        # #121 observed a Nemotron fallback spending thousands of reasoning tokens and
        # ending with finish_reason=length.  These writing contracts need complete JSON,
        # not deep hidden deliberation, so request the smallest normalized reasoning
        # level while retaining reasoning capability for models that require it.
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

    return router._budgeted_provider_call("openrouter", requested_model, do_request)


def install_provider_capacity_hardening() -> None:
    if getattr(router, "_ISCO_PROVIDER_CAPACITY_HARDENED", False):
        return

    original_request_metadata = router._request_metadata

    def hardened_request_metadata(prompt: str) -> dict:
        metadata = original_request_metadata(prompt)
        metadata.update(groq_capacity_estimate(prompt))
        return metadata

    _GROQ_RATE_STATE["remaining_tokens"] = None
    _GROQ_RATE_STATE["reset_at_monotonic"] = None
    router._completion_tokens_for_contract = completion_token_budget
    router._groq_call = _hardened_groq_call
    router._request_metadata = hardened_request_metadata
    router._openrouter_structured_request = _hardened_openrouter_structured_request
    router.RETRY_AFTER_MAX_SECONDS = MAX_RETRY_AFTER_SECONDS
    router._ISCO_PROVIDER_CAPACITY_HARDENED = True
    print(
        "Provider capacity hardening installed: "
        f"groq_tpm={GROQ_FREE_TPM_LIMIT} retry_after_cap={MAX_RETRY_AFTER_SECONDS:g}s "
        f"groq_proactive_pacing=true openrouter_output_models={len(OPENROUTER_OUTPUT_HEAVY_MODELS)}"
    )

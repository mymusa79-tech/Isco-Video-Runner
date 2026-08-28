from __future__ import annotations

import json
import math
import os
import re
import time
from pathlib import Path

from scripts import task_level_planner_router as router

# 8000 is only an initial bootstrap assumption before the first real response from a
# specific Groq model. It is never authoritative after provider evidence is observed.
GROQ_INITIAL_TPM_FALLBACK = 8_000
# Backward-compatibility name retained for tests/importers; runtime admission must call
# groq_effective_tpm_limit()/groq_admission_decision() instead of reading this symbol.
GROQ_FREE_TPM_LIMIT = GROQ_INITIAL_TPM_FALLBACK
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

_DEFAULT_GROQ_MODEL = "openai/gpt-oss-20b"
_GROQ_CAPACITY_SCHEMA = 1
_GROQ_CAPACITY_FILENAME = "groq-model-capacity-v1.json"
_GROQ_MODEL_RATE_STATE: dict[str, dict] = {}
# Legacy view kept for Run124/test compatibility. It mirrors the most recently observed
# model, but no runtime admission decision should use it as model-global authority.
_GROQ_RATE_STATE: dict[str, float | int | None] = {
    "remaining_tokens": None,
    "reset_at_monotonic": None,
}
_DURATION_PART_RE = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h)", flags=re.I)
_LIMIT_PATTERNS = (
    re.compile(r"\blimit(?:\s+on)?\s*(?:tokens\s+per\s+minute|tpm)?\s*[:=]?\s*([\d,]+)\b", re.I),
    re.compile(r"tokens\s+per\s+minute.*?\blimit\s*[:=]?\s*([\d,]+)\b", re.I | re.S),
    re.compile(r"\btpm\b.*?\blimit\s*[:=]?\s*([\d,]+)\b", re.I | re.S),
)


def _capacity_state_path() -> Path | None:
    temp = str(os.environ.get("RUNNER_TEMP") or "").strip()
    if not temp:
        return None
    root = Path(temp) / "isco-state"
    root.mkdir(parents=True, exist_ok=True)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    return root / _GROQ_CAPACITY_FILENAME


def _empty_model_state() -> dict:
    return {
        "contacted": False,
        "actual_tpm_limit": None,
        "remaining_tokens": None,
        "reset_at_epoch": None,
        "blocked_reason": None,
    }


def _load_model_states() -> None:
    path = _capacity_state_path()
    if path is None or not path.is_file():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return
    if not isinstance(payload, dict) or payload.get("schema_version") != _GROQ_CAPACITY_SCHEMA:
        return
    models = payload.get("models")
    if not isinstance(models, dict):
        return
    for model, raw in models.items():
        if not isinstance(model, str) or not isinstance(raw, dict):
            continue
        state = _empty_model_state()
        state["contacted"] = bool(raw.get("contacted"))
        for key in ("actual_tpm_limit", "remaining_tokens"):
            value = raw.get(key)
            if isinstance(value, int) and value >= 0:
                state[key] = value
        reset = raw.get("reset_at_epoch")
        if isinstance(reset, (int, float)) and reset >= 0:
            state["reset_at_epoch"] = float(reset)
        blocked = raw.get("blocked_reason")
        if isinstance(blocked, str) and blocked.strip():
            state["blocked_reason"] = blocked.strip()[:160]
        _GROQ_MODEL_RATE_STATE[model] = state


def _persist_model_states() -> None:
    path = _capacity_state_path()
    if path is None:
        return
    payload = {
        "schema_version": _GROQ_CAPACITY_SCHEMA,
        "models": _GROQ_MODEL_RATE_STATE,
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    tmp.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _model_state(model_name: str) -> dict:
    model = str(model_name or _DEFAULT_GROQ_MODEL).strip() or _DEFAULT_GROQ_MODEL
    if not _GROQ_MODEL_RATE_STATE:
        _load_model_states()
    return _GROQ_MODEL_RATE_STATE.setdefault(model, _empty_model_state())


def reset_groq_capacity_state_for_tests() -> None:
    _GROQ_MODEL_RATE_STATE.clear()
    _GROQ_RATE_STATE["remaining_tokens"] = None
    _GROQ_RATE_STATE["reset_at_monotonic"] = None


def mark_groq_model_unavailable(model_name: str, reason: str) -> None:
    state = _model_state(model_name)
    state["blocked_reason"] = str(reason or "unavailable")[:160]
    state["contacted"] = True
    _persist_model_states()


def groq_model_blocked(model_name: str) -> bool:
    return bool(_model_state(model_name).get("blocked_reason"))


def groq_effective_tpm_limit(model_name: str = _DEFAULT_GROQ_MODEL) -> int | None:
    state = _model_state(model_name)
    actual = state.get("actual_tpm_limit")
    if isinstance(actual, int) and actual > 0:
        return actual
    # The theoretical 8K number is allowed only before first provider contact.
    if state.get("contacted") is not True:
        return GROQ_INITIAL_TPM_FALLBACK
    return None


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


def groq_capacity_estimate(prompt: str, model_name: str = _DEFAULT_GROQ_MODEL) -> dict:
    contract = router._structured_schema_for_prompt(prompt)
    prompt_tokens = estimate_prompt_tokens(prompt)
    reserved_completion = completion_token_budget(contract)
    estimated_total = prompt_tokens + reserved_completion + GROQ_TOKEN_SAFETY_RESERVE
    return {
        "estimated_prompt_tokens": prompt_tokens,
        "reserved_completion_tokens": reserved_completion,
        "token_safety_reserve": GROQ_TOKEN_SAFETY_RESERVE,
        "estimated_request_tokens": estimated_total,
        "provider_tpm_limit": groq_effective_tpm_limit(model_name),
        "provider_model": model_name,
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


def _positive_int(value: object) -> int | None:
    try:
        parsed = int(float(str(value).strip().replace(",", "")))
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _response_error_text(response) -> str:
    try:
        body = response.json()
    except Exception:
        body = None
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            return " ".join(
                str(err.get(key) or "")
                for key in ("code", "type", "message")
            ).strip()
    return ""


def _limit_from_error_text(text: str) -> int | None:
    for pattern in _LIMIT_PATTERNS:
        match = pattern.search(text or "")
        if match:
            value = _positive_int(match.group(1))
            if isinstance(value, int) and value > 0:
                return value
    return None


def observe_groq_response(
    response,
    model_name: str = _DEFAULT_GROQ_MODEL,
    *,
    required_tokens: int | None = None,
) -> dict:
    """Persist model-scoped provider evidence from every real Groq response."""
    state = _model_state(model_name)
    state["contacted"] = True

    limit = _positive_int(_header_value(response.headers, "x-ratelimit-limit-tokens"))
    remaining = _positive_int(_header_value(response.headers, "x-ratelimit-remaining-tokens"))
    reset_seconds = _duration_header_seconds(
        _header_value(response.headers, "x-ratelimit-reset-tokens")
    )
    error_text = _response_error_text(response)

    if limit is None:
        limit = _limit_from_error_text(error_text)
    if isinstance(limit, int) and limit > 0:
        state["actual_tpm_limit"] = limit
    if isinstance(remaining, int):
        state["remaining_tokens"] = remaining
    if reset_seconds is not None:
        state["reset_at_epoch"] = time.time() + reset_seconds

    lower = error_text.lower()
    if (
        ("tokens per day" in lower or "(tpd)" in lower or " tpd" in lower or "daily token" in lower)
        and int(getattr(response, "status_code", 0) or 0) == 429
    ):
        state["blocked_reason"] = "daily_token_quota_exhausted"

    # A previous temporary block should not survive a successful real response.
    if bool(getattr(response, "ok", False)) and state.get("blocked_reason") == "daily_token_quota_exhausted":
        state["blocked_reason"] = None

    _persist_model_states()

    # Mirror current evidence only for old Run124 observability; this is not authority.
    _GROQ_RATE_STATE["remaining_tokens"] = state.get("remaining_tokens")
    _GROQ_RATE_STATE["reset_at_monotonic"] = (
        time.monotonic() + reset_seconds if reset_seconds is not None else None
    )
    return dict(state)


def _update_groq_rate_state(headers, model_name: str = _DEFAULT_GROQ_MODEL) -> None:
    # Compatibility path for tests/legacy callers that only have headers.
    class _HeaderOnlyResponse:
        ok = True
        status_code = 200

        def __init__(self, values):
            self.headers = values

        @staticmethod
        def json():
            return {}

    observe_groq_response(_HeaderOnlyResponse(headers), model_name)


def groq_admission_decision(model_name: str, required_tokens: int) -> dict:
    state = _model_state(model_name)
    required = max(0, int(required_tokens))
    if state.get("blocked_reason"):
        return {
            "action": "unavailable",
            "reason": state["blocked_reason"],
            "required_tokens": required,
            "actual_limit": state.get("actual_tpm_limit"),
            "remaining_tokens": state.get("remaining_tokens"),
        }

    limit = groq_effective_tpm_limit(model_name)
    remaining = state.get("remaining_tokens")
    if isinstance(limit, int) and limit > 0 and required > limit:
        return {
            "action": "impossible",
            "reason": "actual_limit_below_required" if state.get("contacted") else "initial_fallback_below_required",
            "required_tokens": required,
            "actual_limit": limit,
            "remaining_tokens": remaining,
        }
    if isinstance(remaining, int) and required > remaining:
        return {
            "action": "wait",
            "reason": "remaining_below_required",
            "required_tokens": required,
            "actual_limit": limit,
            "remaining_tokens": remaining,
        }
    return {
        "action": "admit" if limit is not None else "unknown",
        "reason": "capacity_available" if limit is not None else "actual_limit_unobserved",
        "required_tokens": required,
        "actual_limit": limit,
        "remaining_tokens": remaining,
    }


def _proactive_groq_pacing(capacity: dict, model_name: str = _DEFAULT_GROQ_MODEL) -> float:
    required = int(capacity["estimated_request_tokens"])
    decision = groq_admission_decision(model_name, required)

    if decision["action"] == "impossible":
        # Critical distinction: an actual model TPM ceiling below the request can never
        # heal with time, so sleeping is strictly forbidden.
        marker = (
            "GROQ_ACTUAL_TPM_BELOW_REQUEST"
            if decision["reason"] == "actual_limit_below_required"
            else "GROQ_TPM_CAPACITY_PREFLIGHT"
        )
        raise RuntimeError(
            f"{marker} model={model_name} required={required} limit={decision['actual_limit']}"
        )
    if decision["action"] == "unavailable":
        raise RuntimeError(
            "GROQ_MODEL_CAPACITY_UNAVAILABLE "
            f"model={model_name} reason={decision['reason']}"
        )
    if decision["action"] != "wait":
        return 0.0

    state = _model_state(model_name)
    reset_at_epoch = state.get("reset_at_epoch")
    if not isinstance(reset_at_epoch, (int, float)):
        # No trustworthy reset evidence: fail over instead of inventing a sleep.
        raise RuntimeError(
            "GROQ_TPM_WINDOW_BUSY_PRECHECK "
            f"model={model_name} required={required} remaining={decision['remaining_tokens']} reset_in=unknown"
        )
    until_reset = max(0.0, float(reset_at_epoch) - time.time())
    delay = min(MAX_RETRY_AFTER_SECONDS, until_reset + GROQ_RATE_RESET_SAFETY_SECONDS)
    if delay <= 0:
        return 0.0
    print(
        "Groq proactive TPM pacing: "
        f"model={model_name} required_estimate={required} "
        f"remaining={decision['remaining_tokens']} delay={delay:.2f}s"
    )
    time.sleep(delay)
    state["remaining_tokens"] = None
    state["reset_at_epoch"] = None
    _persist_model_states()
    _GROQ_RATE_STATE["remaining_tokens"] = None
    _GROQ_RATE_STATE["reset_at_monotonic"] = None
    return delay


def _safe_groq_error(
    response,
    model_name: str = _DEFAULT_GROQ_MODEL,
    *,
    required_tokens: int | None = None,
) -> str:
    state = observe_groq_response(response, model_name, required_tokens=required_tokens)
    if isinstance(required_tokens, int):
        actual = state.get("actual_tpm_limit")
        if isinstance(actual, int) and actual > 0 and required_tokens > actual:
            return (
                "GROQ_ACTUAL_TPM_BELOW_REQUEST "
                f"model={model_name} required={required_tokens} actual_limit={actual} "
                f"status={int(response.status_code)}"
            )
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
    model_name = _DEFAULT_GROQ_MODEL
    request_capacity = groq_capacity_estimate(prompt, model_name=model_name)
    decision = groq_admission_decision(model_name, request_capacity["estimated_request_tokens"])
    if decision["action"] == "impossible":
        raise RuntimeError(
            "GROQ_TPM_CAPACITY_PREFLIGHT "
            f"contract={request_capacity['contract']} "
            f"estimated_prompt_tokens={request_capacity['estimated_prompt_tokens']} "
            f"reserved_completion_tokens={request_capacity['reserved_completion_tokens']} "
            f"safety_tokens={request_capacity['token_safety_reserve']} "
            f"estimated_total={request_capacity['estimated_request_tokens']} "
            f"limit={decision['actual_limit']} model={model_name}"
        )

    _proactive_groq_pacing(request_capacity, model_name=model_name)
    token = router._read_secret_file("GROQ_API_KEY_FILE")
    contract = router._structured_schema_for_prompt(prompt)
    contract_name = contract[0] if contract else "json_object"

    def do_request() -> dict:
        request_payload = {
            "model": model_name,
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
        observe_groq_response(
            response,
            model_name,
            required_tokens=request_capacity["estimated_request_tokens"],
        )
        router._last_call_rate_limit_headers.update(router._extract_rate_limit_headers(response.headers))
        if not response.ok:
            raise RuntimeError(
                _safe_groq_error(
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


def _hardened_openrouter_structured_request(prompt: str, contract: tuple[str, dict]) -> dict:
    schema_name, _schema = contract
    token = router._openrouter_key()
    output_heavy = schema_name in _OUTPUT_HEAVY_CONTRACTS
    requested_model = "openrouter/free-model-fallbacks" if output_heavy else "openrouter/free"

    def do_request() -> dict:
        request_payload = {
            "models": list(OPENROUTER_OUTPUT_HEAVY_MODELS) if output_heavy else list(router._OPENROUTER_MODELS),
            "messages": [{"role": "user", "content": prompt + "\nReturn ONLY one complete valid JSON object. No markdown."}],
            "response_format": _response_format_for_contract(contract),
            "provider": {"allow_fallbacks": True, "require_parameters": True},
            "plugins": [{"id": "response-healing"}],
            "temperature": 0.3,
            "max_tokens": completion_token_budget(contract),
        }
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

    _load_model_states()
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
        f"groq_initial_tpm_fallback={GROQ_INITIAL_TPM_FALLBACK} "
        "groq_model_scoped_dynamic=true "
        f"retry_after_cap={MAX_RETRY_AFTER_SECONDS:g}s "
        f"openrouter_output_models={len(OPENROUTER_OUTPUT_HEAVY_MODELS)}"
    )

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

import isco_video_agent.orchestrator as orchestrator
import isco_video_agent.resilient_planner as staged
from isco_video_agent.ai_budget import (
    AttemptOutcome,
    TaskSpec,
    budget_task_scope,
    get_active_budget_task,
)
from isco_video_agent.providers.gemini import json_text as gemini_json_text
from isco_video_agent.providers.gemini import with_channel_persona
from isco_video_agent.providers.openrouter import json_text as openrouter_json_text
from provider_failure import classify_provider_failure


CACHE_PATH = Path("state/planning-checkpoint.json")
CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)

# One outer retry owner only. SDK/provider-internal retries are not stacked on top.
MIN_PROVIDER_CALL_INTERVAL_SECONDS = 1.5
TRANSIENT_PROVIDER_MAX_ATTEMPTS = 2
TRANSIENT_RETRY_BASE_SECONDS = 1.0
TRANSIENT_RETRY_JITTER_SECONDS = 0.5
TRANSIENT_PROVIDER_COOLDOWN_SECONDS = 30.0
RETRY_AFTER_MAX_SECONDS = 30.0
_TRANSIENT_RESULTS = frozenset({"server_error", "timeout", "network_error", "generation_error"})

# Groq free GPT-OSS currently has an 8K TPM ceiling. We do not pretend this is a
# provider HTTP body limit; it is a conservative local admission boundary introduced
# after Run #115 repeatedly returned HTTP 413 for the large outline request. Large
# requests fail over before spending a Groq call, while later compact repairs remain
# eligible to use Groq. Override only for diagnostics/certification.
GROQ_MAX_PROMPT_UTF8_BYTES = int(os.environ.get("ISCO_GROQ_MAX_PROMPT_UTF8_BYTES", 28 * 1024))

# Gemini (2) + Groq (2) + OpenRouter (2). OpenRouter may use its second attempt only
# for a compact syntax repair of the returned text; it never replays the full prompt.
PLANNING_SUBTASK_MAX_PROVIDER_ATTEMPTS = 6

_USED_PROVIDERS: list[str] = []
_TELEMETRY: list[dict] = []
_last_call_rate_limit_headers: dict = {}
_last_call_response_meta: dict = {}
_CURRENT_REQUEST_META: dict = {}

# Shared with scripts/planning_stage_contract.py's own `_ROUTER_MARKER` constant -
# duplicated here as a bare string literal rather than imported, because that module
# already imports this one (`from scripts import task_level_planner_router as router`),
# so importing it back would be circular. planning_stage_contract.py is the newer,
# more complete Planning provider-loop owner (PlanningStageError taxonomy, explicit
# per-stage admission, structural+semantic validation before the single cache write -
# see its own module docstring). Two full independent "try Gemini, then Groq, then
# OpenRouter" implementations used to both install themselves onto
# isco_video_agent.resilient_planner.json_text, so live behavior depended on which one
# happened to run last. install_router() below checks this marker so that, regardless
# of install order, once the explicit Stage Contract router is live it can never be
# silently replaced by this module's own older provider loop reinstalling itself; see
# test_task_level_planner_router.py's ExplicitStageContractOwnershipTests.
_EXPLICIT_STAGE_CONTRACT_ROUTER_MARKER = "_isco_explicit_planning_contract_router"

_OPENROUTER_FALLBACK_MODELS = ("openai/gpt-oss-20b:free",)
_OPENROUTER_MODELS = ("openrouter/free",) + _OPENROUTER_FALLBACK_MODELS
_OPENROUTER_REPAIR_SUFFIX = "\n\nأعد الرد بصيغة JSON صالحة فقط، بدون أي نص إضافي قبله أو بعده."
_OPENROUTER_COMPACT_REPAIR_MAX_CHARS = 12000


class _OpenRouterMalformedJSON(RuntimeError):
    def __init__(self, raw: str):
        super().__init__("OpenRouter returned invalid JSON after response healing")
        self.raw = raw


def _normalize_provider_name(name: str) -> str:
    return "openrouter" if name.startswith("openrouter") else name


def _record_provider_used(name: str) -> None:
    normalized = _normalize_provider_name(name)
    if normalized not in _USED_PROVIDERS:
        _USED_PROVIDERS.append(normalized)


def get_used_providers() -> list[str]:
    return list(_USED_PROVIDERS)


def get_telemetry() -> list[dict]:
    return list(_TELEMETRY)


def _extract_rate_limit_headers(headers) -> dict:
    return {
        "retry_after": headers.get("Retry-After") or headers.get("retry-after"),
        "remaining_requests": headers.get("X-RateLimit-Remaining-Requests") or headers.get("x-ratelimit-remaining-requests"),
        "remaining_tokens": headers.get("X-RateLimit-Remaining-Tokens") or headers.get("x-ratelimit-remaining-tokens"),
    }


def _extract_response_meta(body: dict, choice: dict) -> dict:
    usage = body.get("usage") if isinstance(body, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    completion_details = usage.get("completion_tokens_details")
    completion_details = completion_details if isinstance(completion_details, dict) else {}
    return {
        "resolved_model": str(body.get("model") or "")[:120] or None,
        "finish_reason": str(choice.get("finish_reason") or "")[:80] or None,
        "native_finish_reason": str(choice.get("native_finish_reason") or "")[:80] or None,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "reasoning_tokens": completion_details.get("reasoning_tokens"),
    }


def _legacy_schema_hint(prompt: str) -> tuple[str, dict] | None:
    """Best-effort schema hint for callers carrying no explicit stage contract.

    The Explicit Planning Stage Contract (planning_stage_contract.py) replaces
    _structured_schema_for_prompt process-wide with a resolver that hard-fails outside
    an active request contract - correct for contract-bound long-form callers (every
    resilient_planner call binds one). task_router itself remains the legacy/
    compatibility provider mesh for callers with no stage contract (the native Short
    planner, via native_short_planner_router.py). A best-effort capacity/telemetry hint
    must never abort their real, successful call - it only ever affects logging and
    local capacity estimates, never the actual request sent to a provider.
    """
    try:
        return _structured_schema_for_prompt(prompt)
    except Exception:
        return None


def _request_metadata(prompt: str) -> dict:
    contract = _legacy_schema_hint(prompt)
    return {
        "prompt_utf8_bytes": len(prompt.encode("utf-8")),
        "response_contract": contract[0] if contract else "json_object",
    }


def _record_attempt(
    provider_name: str,
    result: str,
    *,
    error_detail: str | None = None,
    duration_seconds: float | None = None,
    provider_attempt: int | None = None,
) -> None:
    headers = dict(_last_call_rate_limit_headers)
    _last_call_rate_limit_headers.clear()
    response_meta = dict(_last_call_response_meta)
    _last_call_response_meta.clear()
    entry = {
        "provider": provider_name,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "result": result,
        "error_detail": error_detail,
        "duration_seconds": duration_seconds,
        "retry_after": headers.get("retry_after"),
        "remaining_requests": headers.get("remaining_requests"),
        "remaining_tokens": headers.get("remaining_tokens"),
        "provider_attempt": provider_attempt,
    }
    # Safe metadata only: never store prompt text, secrets, or response bodies.
    entry.update(_CURRENT_REQUEST_META)
    entry.update(response_meta)
    _TELEMETRY.append(entry)


def _record_budget_attempt(
    provider_name: str,
    resolved_model: str,
    outcome: AttemptOutcome,
    *,
    duration_seconds: float,
    detail: str | None = None,
) -> None:
    active = get_active_budget_task()
    if active is None:
        return
    active.ledger.record_attempt(
        active.spec.task_id,
        provider=_normalize_provider_name(provider_name),
        requested_model=active.requested_model,
        resolved_model=resolved_model,
        capability=active.spec.capability,
        outcome=outcome,
        duration_seconds=duration_seconds,
        detail=detail,
    )


def _budgeted_provider_call(provider_name: str, resolved_model: str, call, *args, **kwargs):
    """Account for exactly one provider callable invocation."""
    active = get_active_budget_task()
    if active is None:
        return call(*args, **kwargs)
    if not active.ledger.authorize(active.spec.task_id):
        raise RuntimeError(f"AI budget authorization denied for task {active.spec.task_id}; provider call blocked")
    started = time.monotonic()
    try:
        result = call(*args, **kwargs)
    except Exception as exc:
        failure = classify_provider_failure(provider_name, exc)
        _record_budget_attempt(
            provider_name,
            resolved_model,
            failure.budget_outcome,
            duration_seconds=time.monotonic() - started,
            detail=str(exc)[:220],
        )
        raise
    _record_budget_attempt(
        provider_name,
        resolved_model,
        AttemptOutcome.SUCCESS,
        duration_seconds=time.monotonic() - started,
    )
    return result


def _summarize_telemetry_by_provider(attempts: list[dict]) -> dict:
    summary: dict = {}
    for entry in attempts:
        name = entry["provider"]
        bucket = summary.setdefault(name, {"total_attempts": 0, "by_result": {}})
        bucket["total_attempts"] += 1
        bucket["by_result"][entry["result"]] = bucket["by_result"].get(entry["result"], 0) + 1
    return summary


def write_planning_telemetry(out_dir: Path) -> Path:
    attempts = list(_TELEMETRY)
    payload = {
        "schema_version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "providers": _summarize_telemetry_by_provider(attempts),
        "attempts": attempts,
    }
    path = out_dir / "planning-telemetry.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _read_secret_file(name: str) -> str:
    path = Path(os.environ[name])
    return path.read_text(encoding="utf-8").strip()


def _load_checkpoint() -> dict:
    if not CACHE_PATH.exists():
        return {"version": 1, "responses": {}}
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "responses": {}}
    if not isinstance(data, dict):
        return {"version": 1, "responses": {}}
    data.setdefault("version", 1)
    data.setdefault("responses", {})
    return data


def _save_checkpoint(data: dict) -> None:
    tmp = CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CACHE_PATH)


def _parse_json(raw):
    if isinstance(raw, dict):
        return raw
    text = str(raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        value = None
        for i, ch in enumerate(text):
            if ch != "{":
                continue
            try:
                candidate, _ = decoder.raw_decode(text[i:])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                value = candidate
                break
        if value is None:
            raise RuntimeError("Provider returned invalid JSON")
    if not isinstance(value, dict):
        raise RuntimeError("Provider JSON must be an object")
    return value


def _expected_sections(prompt: str) -> int | None:
    match = re.search(r"Required number of sections:\s*exactly\s*(\d+)", prompt)
    return int(match.group(1)) if match else None


def _strict_object(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


def _string_array(*, min_items: int | None = None, max_items: int | None = None) -> dict:
    schema: dict = {"type": "array", "items": {"type": "string"}}
    if min_items is not None:
        schema["minItems"] = min_items
    if max_items is not None:
        schema["maxItems"] = max_items
    return schema


def _outline_response_schema(expected: int) -> dict:
    editorial_intent = _strict_object(
        {
            "editorial_thesis": {"type": "string"},
            "viewer_starting_belief": {"type": "string"},
            "hidden_assumption": {"type": "string"},
            "editorial_turn": {"type": "string"},
            "stakes": {"type": "string"},
            "viewer_promise": {"type": "string"},
            "evidence_boundaries": _string_array(min_items=1, max_items=5),
            "earned_payoff": {"type": "string"},
        },
        [
            "editorial_thesis", "viewer_starting_belief", "hidden_assumption", "editorial_turn",
            "stakes", "viewer_promise", "evidence_boundaries", "earned_payoff",
        ],
    )
    brief = _strict_object(
        {
            "id": {"type": "string"}, "purpose": {"type": "string"}, "visual_query": {"type": "string"},
            "on_screen_text": {"type": "string"}, "emotion": {"type": "string"}, "expected_seconds": {"type": "number"},
        },
        ["id", "purpose", "visual_query", "on_screen_text", "emotion", "expected_seconds"],
    )
    return _strict_object(
        {
            "pillar": {"type": "string"}, "hook": {"type": "string"},
            "title_options": _string_array(min_items=3, max_items=3),
            "thumbnail_concepts": _string_array(min_items=3, max_items=3),
            "cta": {"type": "string"}, "closing_payoff": {"type": "string"},
            "narrative_format": {"type": "string"}, "opener_variant": {"type": "string"},
            "closer_variant": {"type": "string"}, "transition_variants": _string_array(min_items=3, max_items=3),
            "editorial_intent": editorial_intent,
            "section_briefs": {"type": "array", "items": brief, "minItems": expected, "maxItems": expected},
        },
        [
            "pillar", "hook", "title_options", "thumbnail_concepts", "cta", "closing_payoff",
            "narrative_format", "opener_variant", "closer_variant", "transition_variants",
            "editorial_intent", "section_briefs",
        ],
    )


def _structured_schema_for_prompt(prompt: str) -> tuple[str, dict] | None:
    expected = _expected_sections(prompt)
    if expected is not None and "section_briefs" in prompt:
        return "editorial_outline", _outline_response_schema(expected)

    exact_match = re.search(r"with EXACTLY\s*(\d+)\s+entries", prompt, flags=re.I)
    exact_count = int(exact_match.group(1)) if exact_match else None
    if exact_count is not None and '"sections"' in prompt:
        item = _strict_object(
            {"id": {"type": "string"}, "narration": {"type": "string"}, "key_point": {"type": "string"}},
            ["id", "narration", "key_point"],
        )
        return "full_script", _strict_object(
            {"sections": {"type": "array", "items": item, "minItems": exact_count, "maxItems": exact_count}},
            ["sections"],
        )
    if exact_count is not None and '"additions"' in prompt:
        item = _strict_object(
            {"id": {"type": "string"}, "append_text": {"type": "string"}},
            ["id", "append_text"],
        )
        return "append_only_repair", _strict_object(
            {"additions": {"type": "array", "items": item, "minItems": exact_count, "maxItems": exact_count}},
            ["additions"],
        )
    if 'Return ONLY JSON: {"narration"' in prompt:
        return "section_repair", _strict_object({"narration": {"type": "string"}}, ["narration"])
    return None


def _normalize_outline(data: dict, prompt: str) -> dict:
    expected = _expected_sections(prompt)
    if expected is None or "section_briefs" not in data:
        return data
    briefs = data.get("section_briefs")
    if not isinstance(briefs, list):
        raise RuntimeError("Outline section_briefs must be a list")
    valid = [b for b in briefs if isinstance(b, dict) and str(b.get("purpose", "")).strip()]
    if len(valid) > expected:
        fixed = dict(data)
        fixed["section_briefs"] = valid[:expected]
        print(f"Outline normalized locally: trimmed {len(valid)} to {expected}")
        return fixed
    if len(valid) < expected:
        raise RuntimeError(f"Outline missing sections: got {len(valid)}, need {expected}")
    fixed = dict(data)
    fixed["section_briefs"] = valid
    return fixed


def _enrich_dialogue_prompt(prompt: str) -> str:
    lower = prompt.lower()
    if "dialogue_qa" not in lower:
        return prompt
    if "write only this section" not in lower and "repair one section only" not in lower:
        return prompt
    return prompt + """

DIALOGUE VOICE CONTRACT (mandatory when the selected narrative format is dialogue_qa):
- Write the spoken exchange using only these exact Arabic role labels at the start of turns: "السائل:" and "المجيب:".
- Use both roles in this section when natural; normally 2-6 short turns. Never invent names, host/guest pleasantries, or stage directions.
- السائل asks/challenges with genuine curiosity and may push back briefly; المجيب remains the channel's main analytical voice.
- The exchange must advance the argument. Do not make both speakers say the same idea.
- Labels are routing metadata for TTS and will not be spoken aloud.
- Preserve these labels during repair. Do not return an unlabelled dialogue monologue.
"""


def _safe_api_error(prefix: str, response: requests.Response) -> str:
    status = int(response.status_code)
    code = ""
    message = ""
    provider = ""
    try:
        body = response.json()
    except Exception:
        body = None
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            code = str(err.get("code") or err.get("type") or "").strip()[:80]
            message = str(err.get("message") or "").strip()[:220]
            meta = err.get("metadata")
            if isinstance(meta, dict):
                provider = str(meta.get("provider_name") or meta.get("provider") or "").strip()[:80]
    parts = [f"{prefix}_HTTP_{status}", f"status={status}"]
    if code:
        parts.append(f"code={code}")
    if provider:
        parts.append(f"provider={provider}")
    if message:
        parts.append(f"message={message}")
    return " ".join(parts)


def _completion_tokens_for_contract(contract: tuple[str, dict] | None) -> int:
    name = contract[0] if contract else "json_object"
    return {
        "editorial_outline": 3200,
        "full_script": 4800,
        "append_only_repair": 3200,
        "section_repair": 2200,
    }.get(name, 2600)


def _groq_call(prompt: str) -> dict:
    # Admission control is request-scoped and intentionally before BudgetLedger
    # authorization because no provider call is made when the request is known risky.
    prompt_bytes = len(prompt.encode("utf-8"))
    if prompt_bytes > GROQ_MAX_PROMPT_UTF8_BYTES:
        raise RuntimeError(
            f"GROQ_PAYLOAD_TOO_LARGE_PREFLIGHT prompt_bytes={prompt_bytes} limit={GROQ_MAX_PROMPT_UTF8_BYTES}"
        )

    token = _read_secret_file("GROQ_API_KEY_FILE")
    contract = _legacy_schema_hint(prompt)

    def do_request() -> dict:
        response_format: dict
        if contract is None:
            response_format = {"type": "json_object"}
        else:
            schema_name, schema = contract
            response_format = {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            }
        request_payload = {
            "model": "openai/gpt-oss-20b",
            "messages": [{"role": "user", "content": prompt + "\nReturn ONLY one complete valid JSON object. No markdown."}],
            "response_format": response_format,
            "temperature": 0.15,
            "max_completion_tokens": _completion_tokens_for_contract(contract),
        }
        # GPT-OSS reasoning tokens share the completion budget.  For the bounded
        # outline schema, low effort preserves reasoning while reserving room for the
        # complete JSON instead of reproducing Run #116's finish_reason=length.
        if contract is not None and contract[0] == "editorial_outline":
            request_payload["reasoning_effort"] = "low"
        response = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
            json=request_payload,
            timeout=90,
        )
        _last_call_rate_limit_headers.update(_extract_rate_limit_headers(response.headers))
        if not response.ok:
            raise RuntimeError(_safe_api_error("GROQ", response))
        body = response.json()
        choices = body.get("choices") or []
        if not choices:
            raise RuntimeError("Groq returned no choices")
        choice = choices[0]
        _last_call_response_meta.update(_extract_response_meta(body, choice))
        finish = str(choice.get("finish_reason") or "").strip().lower()
        if finish in {"length", "max_tokens"}:
            raise RuntimeError("GROQ_PREMATURE_RESPONSE finish_reason=length")
        content = str((choice.get("message") or {}).get("content") or "")
        if not content.strip():
            raise RuntimeError("GROQ_EMPTY_OUTPUT")
        return _parse_json(content)

    return _budgeted_provider_call("groq", "openai/gpt-oss-20b", do_request)


def _openrouter_key() -> str:
    return _read_secret_file("OPENROUTER_API_KEY_FILE")


def _openrouter_structured_request(prompt: str, contract: tuple[str, dict]) -> dict:
    schema_name, schema = contract
    token = _openrouter_key()

    def do_request() -> dict:
        request_payload = {
            "models": list(_OPENROUTER_MODELS),
            "messages": [{"role": "user", "content": prompt + "\nReturn ONLY one complete valid JSON object. No markdown."}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            },
            "provider": {"allow_fallbacks": True, "require_parameters": True},
            "plugins": [{"id": "response-healing"}],
            "temperature": 0.3,
            "max_tokens": _completion_tokens_for_contract(contract),
        }
        if schema_name == "editorial_outline":
            request_payload["reasoning"] = {"effort": "low", "exclude": True}
        response = requests.post(
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
        _last_call_rate_limit_headers.update(_extract_rate_limit_headers(response.headers))
        if not response.ok:
            raise RuntimeError(_safe_api_error("OPENROUTER", response))
        body = response.json()
        choices = body.get("choices") or []
        if not choices:
            raise RuntimeError("OpenRouter returned no choices")
        choice = choices[0]
        _last_call_response_meta.update(_extract_response_meta(body, choice))
        finish = str(choice.get("finish_reason") or "").strip().lower()
        if finish in {"length", "max_tokens"}:
            raise RuntimeError("OPENROUTER_PREMATURE_RESPONSE finish_reason=length")
        content = str((choice.get("message") or {}).get("content") or "")
        if not content.strip():
            raise RuntimeError("OPENROUTER_EMPTY_OUTPUT")
        try:
            return _parse_json(content)
        except RuntimeError as exc:
            if "invalid JSON" not in str(exc):
                raise
            raise _OpenRouterMalformedJSON(content) from None

    return _budgeted_provider_call("openrouter", "openrouter/free", do_request)


def _compact_openrouter_repair_prompt(raw: str) -> str:
    clipped = raw[:_OPENROUTER_COMPACT_REPAIR_MAX_CHARS]
    return (
        "Repair ONLY the JSON syntax/shape of the provider output below. Preserve its existing meaning and values. "
        "Do not invent missing editorial content. Return one complete JSON object only.\n\n"
        "MALFORMED_OUTPUT:\n" + clipped
    )


def _openrouter_call_with_repair(
    prompt: str,
    model: str,
    provider_name: str = "openrouter",
    *,
    response_contract: tuple[str, dict] | None = None,
) -> dict:
    """OpenRouter request with no full-prompt replay.

    Known production contracts use native strict schema + provider/model failover +
    free Response Healing. If syntax is still malformed, exactly one *compact* repair
    is allowed using only the returned text; truncation never enters this repair path.
    Unknown/legacy JSON tasks retain the prior adapter behavior for compatibility.
    """
    if response_contract is not None:
        try:
            return _openrouter_structured_request(prompt, response_contract)
        except _OpenRouterMalformedJSON as exc:
            try:
                return _openrouter_structured_request(
                    _compact_openrouter_repair_prompt(exc.raw), response_contract
                )
            except _OpenRouterMalformedJSON:
                raise RuntimeError("OpenRouter returned invalid JSON after bounded response healing/repair") from None

    try:
        return _budgeted_provider_call(provider_name, model, openrouter_json_text, prompt, model=model)
    except RuntimeError as exc:
        if "invalid JSON" not in str(exc):
            raise
        return _budgeted_provider_call(
            provider_name,
            model,
            openrouter_json_text,
            prompt + _OPENROUTER_REPAIR_SUFFIX,
            model=model,
        )


def _retry_after_seconds(value: object) -> float | None:
    try:
        seconds = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if seconds < 0:
        return None
    return min(seconds, RETRY_AFTER_MAX_SECONDS)


def _retry_delay_seconds(provider_name: str, retry_index: int, retry_after: object = None) -> float:
    digest = hashlib.sha256(f"{provider_name}:{retry_index}".encode("utf-8")).digest()
    fraction = int.from_bytes(digest[:2], "big") / 65535.0
    exponential = TRANSIENT_RETRY_BASE_SECONDS * (2 ** retry_index)
    calculated = exponential + (fraction * TRANSIENT_RETRY_JITTER_SECONDS)
    header_delay = _retry_after_seconds(retry_after)
    return max(calculated, header_delay or 0.0)


def install_router() -> None:
    checkpoint = _load_checkpoint()
    responses = checkpoint.setdefault("responses", {})
    cooldown: set[str] = set()
    transient_cooldown_until: dict[str, float] = {}
    last_call_at: dict[str, float] = {}
    planning_subtask_sequence = 0
    _USED_PROVIDERS.clear()
    _TELEMETRY.clear()
    _CURRENT_REQUEST_META.clear()
    _last_call_response_meta.clear()

    providers = [
        (
            "gemini",
            lambda api_key, prompt, model: _budgeted_provider_call(
                "gemini", model, gemini_json_text, api_key, prompt, model=model
            ),
        ),
        ("groq", lambda _api_key, prompt, model: _groq_call(prompt)),
        (
            "openrouter",
            lambda _api_key, prompt, model: _openrouter_call_with_repair(
                prompt,
                "openrouter/free",
                "openrouter",
                response_contract=_legacy_schema_hint(prompt),
            ),
        ),
    ]

    def task_router(api_key, prompt, model="gemini-2.5-flash"):
        nonlocal planning_subtask_sequence
        prompt = _enrich_dialogue_prompt(prompt)
        prompt = with_channel_persona(prompt)
        cache_key = hashlib.sha256((model + "\n" + prompt).encode("utf-8")).hexdigest()
        cached = responses.get(cache_key)
        if isinstance(cached, dict):
            print("Planning checkpoint hit")
            return cached

        _CURRENT_REQUEST_META.clear()
        _CURRENT_REQUEST_META.update(_request_metadata(prompt))

        def run_provider_loop():
            failures: list[str] = []
            for name, provider in providers:
                if name in cooldown:
                    _record_attempt(name, "circuit-open")
                    continue

                cooldown_until = transient_cooldown_until.get(name)
                if cooldown_until is not None and cooldown_until > time.monotonic():
                    _record_attempt(name, "transient-cooldown")
                    continue

                for provider_attempt in range(TRANSIENT_PROVIDER_MAX_ATTEMPTS):
                    since_last_call = time.monotonic() - last_call_at.get(name, 0.0)
                    if since_last_call < MIN_PROVIDER_CALL_INTERVAL_SECONDS:
                        time.sleep(MIN_PROVIDER_CALL_INTERVAL_SECONDS - since_last_call)
                    last_call_at[name] = time.monotonic()
                    try:
                        raw = provider(api_key, prompt, model)
                        data = _normalize_outline(_parse_json(raw), prompt)
                        responses[cache_key] = data
                        checkpoint["last_provider"] = name
                        try:
                            _save_checkpoint(checkpoint)
                        except Exception as save_exc:
                            # Cross-run checkpoint persistence is a caching optimization,
                            # not a correctness gate - planning_legacy_authority_guard
                            # deliberately seals it once the Explicit Planning Stage
                            # Contract owns checkpointing, for callers still on this
                            # legacy/compatibility provider mesh (the native Short
                            # planner). A sealed persistence path must never discard an
                            # already-successful provider result.
                            print(f"Planning checkpoint persistence skipped: {save_exc}")
                        _record_provider_used(name)
                        _record_attempt(
                            name,
                            "success",
                            duration_seconds=time.monotonic() - last_call_at[name],
                            provider_attempt=provider_attempt + 1,
                        )
                        print(f"Planning subtask provider selected: {name}")
                        return data
                    except Exception as exc:
                        detail = str(exc).replace("\n", " ")[:220]
                        failure = classify_provider_failure(name, exc)
                        retry_after = _last_call_rate_limit_headers.get("retry_after")
                        _record_attempt(
                            name,
                            failure.telemetry_result,
                            error_detail=detail,
                            duration_seconds=time.monotonic() - last_call_at[name],
                            provider_attempt=provider_attempt + 1,
                        )

                        retryable = (
                            failure.telemetry_result in _TRANSIENT_RESULTS
                            or (failure.telemetry_result == "429" and retry_after is not None)
                        )
                        has_retry = provider_attempt + 1 < TRANSIENT_PROVIDER_MAX_ATTEMPTS
                        if retryable and has_retry:
                            delay = _retry_delay_seconds(name, provider_attempt, retry_after)
                            print(
                                "Planning provider transient retry: "
                                f"{name} result={failure.telemetry_result} "
                                f"attempt={provider_attempt + 1}/{TRANSIENT_PROVIDER_MAX_ATTEMPTS} "
                                f"delay={delay:.2f}s"
                            )
                            time.sleep(delay)
                            continue

                        failures.append(f"{name}:{detail}")
                        if failure.open_circuit:
                            cooldown.add(name)
                            print(f"Planning provider circuit-open for this run: {name}")
                        elif retryable:
                            transient_cooldown_until[name] = time.monotonic() + TRANSIENT_PROVIDER_COOLDOWN_SECONDS
                            print(
                                "Planning provider transient cooldown: "
                                f"{name} seconds={TRANSIENT_PROVIDER_COOLDOWN_SECONDS:g}"
                            )
                        else:
                            print(f"Planning subtask failed safely: {name}:{detail}")
                        break

            raise RuntimeError("All free providers failed for planning subtask: " + " | ".join(failures))

        active = get_active_budget_task()
        if active is None or active.spec.kind != "OUTLINE_PLAN":
            return run_provider_loop()

        planning_subtask_sequence += 1
        child = TaskSpec(
            task_id=f"{active.spec.task_id}_{active.spec.priority.name}_SUBTASK_{planning_subtask_sequence:03d}",
            kind="PLANNING_SUBTASK",
            priority=active.spec.priority,
            capability=active.spec.capability,
            max_provider_attempts=PLANNING_SUBTASK_MAX_PROVIDER_ATTEMPTS,
            schema_repair_allowed=active.spec.schema_repair_allowed,
            local_fallback=False,
            semantic_block_is_final=False,
        )
        with budget_task_scope(active.ledger, child, requested_model=active.requested_model):
            return run_provider_loop()

    def routed_build_plan(*args, **kwargs):
        plan = staged.build_plan(*args, **kwargs)
        if getattr(plan, "narrative_format", "") == "dialogue_qa":
            os.environ["ISCO_DIALOGUE_QA"] = "1"
            print("Dialogue voice mode selected: questioner=Iapetus responder=Gacrux")
        else:
            os.environ.pop("ISCO_DIALOGUE_QA", None)
        return plan

    routed_build_plan._is_resilient_router = True
    # Never clobber the newer, more complete explicit Stage Contract router if it is
    # already the live isco_video_agent.resilient_planner.json_text owner - see the
    # _EXPLICIT_STAGE_CONTRACT_ROUTER_MARKER comment above. Every other install_router()
    # side effect (checkpoint bootstrap, telemetry reset, the routed_build_plan
    # dialogue_qa wrapper) still runs unconditionally; only this module's own provider
    # loop is skipped when it would not be reachable anyway.
    if not getattr(staged.json_text, _EXPLICIT_STAGE_CONTRACT_ROUTER_MARKER, False):
        staged.json_text = task_router
    orchestrator.build_plan = routed_build_plan


def run() -> None:
    install_router()
    request = json.loads(Path(os.environ["REQUEST_FILE"]).read_text(encoding="utf-8"))
    out = orchestrator.produce(
        topic=request["topic"],
        requested_format=request["format"],
        dry_run=False,
        do_research=True,
    )
    print(f"Production completed: {out.name}")


if __name__ == "__main__":
    run()

from __future__ import annotations

import json
import os
from pathlib import Path

import isco_video_agent.resilient_planner as staged

from scripts import planning_batch_hardening as batching
from scripts import provider_capacity_hardening as capacity
from scripts import task_level_planner_router as router


# Run #125 proved three independent transport facts:
# 1) the Run124 bounded terminal reset recovery works, but repeated writer/doctor
#    prompts put variable batch data before the large shared prefix, defeating Groq's
#    automatic prompt cache and therefore burning TPM/TPD on the same policy/research;
# 2) openai/gpt-oss-20b can exhaust its free 200K TPD during repeated recovery runs;
# 3) an OpenRouter key already marked blocked by preflight still received many
#    output-heavy calls, all ending at finish_reason=length.
#
# This closure changes transport/routing only. It does not alter the approved topic,
# prompt wording, JSON schemas, section word gates, cultural/factuality rules, provider
# attempt ledger, final critic, visual gates, audio gates, or release gates.
_CACHE_LAYOUT_MARKER = "ISCO_CACHE_FRIENDLY_LAYOUT_V1"
_CACHEABLE_GROQ_MODELS = frozenset({"openai/gpt-oss-20b", "openai/gpt-oss-120b"})
_GROQ_MODEL_POOL = (
    "openai/gpt-oss-20b",
    "openai/gpt-oss-120b",
    "qwen/qwen3.8-27b",
)
_ACTIVE_GROQ_INDEX = 0
_WARM_CACHE_GROUPS: set[tuple[str, str]] = set()
_OPENROUTER_STRUCTURAL_BLOCKED = False
_INSTALLED = False


def _extract_block(text: str, start_marker: str, end_marker: str) -> tuple[str, str]:
    start = text.find(start_marker)
    if start < 0:
        return text, ""
    end = text.find(end_marker, start + len(start_marker))
    if end < 0:
        return text, ""
    block = text[start:end]
    return text[:start] + text[end:], block


def _writer_cache_layout(prompt: str) -> str:
    text = prompt
    dynamic: list[str] = []

    # Batch range is the first changing field in the old prompt and therefore destroys
    # the exact common prefix. Move it behind all immutable editorial context.
    lines = text.splitlines(keepends=True)
    if len(lines) >= 2 and lines[1].startswith("Write ONLY global sections "):
        dynamic.append(lines.pop(1))
        text = "".join(lines)

    text, block = _extract_block(
        text,
        "PREVIOUS_WRITTEN_KEY_POINTS (context only; do not repeat their role):",
        "FOLLOWING_SECTION_PURPOSES (context only; do not steal their payoff):",
    )
    if block:
        dynamic.append(block)
    text, block = _extract_block(
        text,
        "FOLLOWING_SECTION_PURPOSES (context only; do not steal their payoff):",
        "Hard writing rules for every returned section:",
    )
    if block:
        dynamic.append(block)
    text, block = _extract_block(text, "GLOBAL POSITION RULES:", "EDITORIAL_POLICY:")
    if block:
        dynamic.append(block)

    batch_start = text.find("BATCH_SECTION_SPECS — write exactly one narration per entry in this exact order:")
    if batch_start >= 0:
        dynamic.append(text[batch_start:])
        text = text[:batch_start]

    if not dynamic:
        return prompt
    return (
        text.rstrip()
        + f"\n\n{_CACHE_LAYOUT_MARKER}\n"
        + "DYNAMIC_BATCH_CONTEXT — transport-specific values follow the shared cached prefix:\n"
        + "\n".join(part.strip() for part in dynamic if part.strip())
        + "\n"
    )


def _doctor_cache_layout(prompt: str) -> str:
    text = prompt
    dynamic: list[str] = []

    # The old doctor prompt changes on line two. Preserve exactly the same instruction,
    # but move that range declaration after the shared policy/research/key-point state.
    start = text.find("Repair ONE BOUNDED BATCH")
    topic = text.find("Topic:", start if start >= 0 else 0)
    if start >= 0 and topic > start:
        dynamic.append(text[start:topic])
        text = text[:start] + text[topic:]

    batch_start = text.find("BATCH_SECTIONS:")
    if batch_start >= 0:
        dynamic.append(text[batch_start:])
        text = text[:batch_start]

    if not dynamic:
        return prompt
    return (
        text.rstrip()
        + f"\n\n{_CACHE_LAYOUT_MARKER}\n"
        + "DYNAMIC_BATCH_CONTEXT — transport-specific values follow the shared cached prefix:\n"
        + "\n".join(part.strip() for part in dynamic if part.strip())
        + "\n"
    )


def cache_friendly_prompt(prompt: str, label: str) -> str:
    """Reorder existing text only; no instruction is deleted or rewritten."""
    if _CACHE_LAYOUT_MARKER in prompt:
        return prompt
    if label == "writer":
        return _writer_cache_layout(prompt)
    if label == "doctor":
        return _doctor_cache_layout(prompt)
    return prompt


def _cache_group(contract_name: object) -> str | None:
    name = str(contract_name or "")
    if name.startswith("script_writer_"):
        return "writer"
    if name.startswith("script_doctor_"):
        return "doctor"
    return None


def _is_tpd_exhausted(error: BaseException | str) -> bool:
    lower = str(error).lower()
    return (
        "tokens per day" in lower
        or "(tpd)" in lower
        or " tpd:" in lower
        or "daily token" in lower
    ) and ("429" in lower or "rate_limit" in lower or "rate limit" in lower)


def _is_model_unavailable(error: BaseException | str) -> bool:
    lower = str(error).lower()
    return (
        "model_not_found" in lower
        or "model not found" in lower
        or "does not exist" in lower
        or "not available" in lower
    )


def _active_groq_model() -> str:
    return _GROQ_MODEL_POOL[_ACTIVE_GROQ_INDEX]


def _switch_groq_model(reason: str) -> bool:
    global _ACTIVE_GROQ_INDEX
    if _ACTIVE_GROQ_INDEX + 1 >= len(_GROQ_MODEL_POOL):
        return False
    previous = _GROQ_MODEL_POOL[_ACTIVE_GROQ_INDEX]
    _ACTIVE_GROQ_INDEX += 1
    current = _GROQ_MODEL_POOL[_ACTIVE_GROQ_INDEX]
    capacity._GROQ_RATE_STATE["remaining_tokens"] = None
    capacity._GROQ_RATE_STATE["reset_at_monotonic"] = None
    router._last_call_rate_limit_headers.clear()
    router._last_call_response_meta.clear()
    print(f"Run125 Groq model failover: {previous} -> {current} reason={reason}")
    return True


def _groq_model_call(prompt: str, model_name: str) -> dict:
    request_capacity = capacity.groq_capacity_estimate(prompt)
    if request_capacity["estimated_request_tokens"] > capacity.GROQ_FREE_TPM_LIMIT:
        raise RuntimeError(
            "GROQ_TPM_CAPACITY_PREFLIGHT "
            f"contract={request_capacity['contract']} "
            f"estimated_total={request_capacity['estimated_request_tokens']} "
            f"limit={capacity.GROQ_FREE_TPM_LIMIT}"
        )

    capacity._proactive_groq_pacing(request_capacity)
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
        if model_name.startswith("qwen/"):
            payload["reasoning_effort"] = "none"
            payload["include_reasoning"] = False
        elif contract_name == "editorial_outline" or contract_name in capacity._OUTPUT_HEAVY_CONTRACTS:
            payload["reasoning_effort"] = "low"
            payload["include_reasoning"] = False

        response = router.requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
            json=payload,
            timeout=90,
        )
        capacity._update_groq_rate_state(response.headers)
        router._last_call_rate_limit_headers.update(router._extract_rate_limit_headers(response.headers))
        if not response.ok:
            raise RuntimeError(capacity._safe_groq_error(response))
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


def _provider_preflight_path() -> Path | None:
    explicit = str(os.environ.get("ISCO_PROVIDER_PREFLIGHT_PATH") or "").strip()
    if explicit:
        return Path(explicit)
    temp = str(os.environ.get("RUNNER_TEMP") or "").strip()
    return Path(temp) / "provider-preflight.json" if temp else None


def openrouter_preflight_blocked(path: Path | None = None) -> bool:
    target = path or _provider_preflight_path()
    if target is None or not target.is_file():
        return False
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    for check in data.get("checks") or []:
        if not isinstance(check, dict):
            continue
        if str(check.get("provider") or "").strip().lower() != "openrouter":
            continue
        return str(check.get("status") or "").strip().lower() == "block"
    return False


def install_run125_capacity_routing_closure() -> None:
    global _INSTALLED, _OPENROUTER_STRUCTURAL_BLOCKED
    if _INSTALLED:
        return

    # 1) Keep the exact Writer/Doctor instructions but move dynamic shard values to the
    # tail, matching Groq's documented prefix-cache layout.
    original_shard = batching._call_capacity_aware_shard

    def cache_layout_shard(api_key: str, model: str, ids: list[str], *, prompt_builder, label: str):
        def cache_builder(child_ids: list[str]) -> str:
            return cache_friendly_prompt(prompt_builder(child_ids), label)

        return original_shard(
            api_key,
            model,
            ids,
            prompt_builder=cache_builder,
            label=label,
        )

    batching._call_capacity_aware_shard = cache_layout_shard

    # 2) Expose cache-hit telemetry and warm a family after its first successful Groq
    # call. Once warm, do not reject the next same-family call solely from a local
    # full-prompt estimate; the provider is the authority on cached-token accounting.
    original_meta = router._extract_response_meta

    def cache_meta(body: dict, choice: dict) -> dict:
        meta = original_meta(body, choice)
        usage = body.get("usage") if isinstance(body, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        details = usage.get("prompt_tokens_details")
        details = details if isinstance(details, dict) else {}
        meta["cached_tokens"] = details.get("cached_tokens")
        return meta

    router._extract_response_meta = cache_meta

    original_record = router._record_attempt

    def cache_record(provider_name: str, result: str, **kwargs) -> None:
        if str(provider_name).startswith("groq") and result == "success":
            group = _cache_group(router._CURRENT_REQUEST_META.get("response_contract"))
            model_name = _active_groq_model()
            if group and model_name in _CACHEABLE_GROQ_MODELS:
                _WARM_CACHE_GROUPS.add((model_name, group))
        original_record(provider_name, result, **kwargs)

    router._record_attempt = cache_record

    original_pacing = capacity._proactive_groq_pacing

    def cache_aware_pacing(request_capacity: dict) -> float:
        group = _cache_group(request_capacity.get("contract"))
        model_name = _active_groq_model()
        if group and (model_name, group) in _WARM_CACHE_GROUPS:
            remaining = capacity._GROQ_RATE_STATE.get("remaining_tokens")
            required = int(request_capacity.get("estimated_request_tokens") or 0)
            if isinstance(remaining, int) and required > remaining:
                print(
                    "Run125 Groq cache-aware probe: "
                    f"model={model_name} group={group} required_full_estimate={required} "
                    f"remaining_window={remaining} action=provider_authoritative_cache_accounting"
                )
                return 0.0
        return original_pacing(request_capacity)

    capacity._proactive_groq_pacing = cache_aware_pacing

    # 3) Hard daily quota exhaustion is model-scoped. Move to another Groq model that
    # supports the same structured-output contract instead of retrying a dead TPD bucket.
    original_groq = router._groq_call

    def groq_model_pool(prompt: str) -> dict:
        while True:
            model_name = _active_groq_model()
            try:
                if model_name == _GROQ_MODEL_POOL[0]:
                    return original_groq(prompt)
                return _groq_model_call(prompt, model_name)
            except Exception as exc:
                if _is_tpd_exhausted(exc):
                    if _switch_groq_model("daily_token_quota_exhausted"):
                        continue
                if _is_model_unavailable(exc):
                    if _switch_groq_model("model_unavailable"):
                        continue
                raise

    router._groq_call = groq_model_pool

    # 4) Honor the preflight result. If OpenRouter is unavailable, make one local
    # circuit-opening signal with zero HTTP calls. If it is nominally available but one
    # output-heavy request truncates, block subsequent requests for this run rather than
    # repeating the same structural failure on every shard.
    _OPENROUTER_STRUCTURAL_BLOCKED = openrouter_preflight_blocked()
    original_openrouter = router._openrouter_structured_request

    def bounded_openrouter(prompt: str, contract: tuple[str, dict]) -> dict:
        global _OPENROUTER_STRUCTURAL_BLOCKED
        if _OPENROUTER_STRUCTURAL_BLOCKED:
            raise RuntimeError("OPENROUTER_NO_PROVIDER_AVAILABLE run125_preflight_or_structural_block")
        try:
            return original_openrouter(prompt, contract)
        except Exception as exc:
            lower = str(exc).lower()
            if "premature_response" in lower or "finish_reason=length" in lower:
                _OPENROUTER_STRUCTURAL_BLOCKED = True
                print("Run125 OpenRouter structural circuit armed after first truncated output")
            raise

    router._openrouter_structured_request = bounded_openrouter

    _INSTALLED = True
    print(
        "Run125 capacity routing closure installed: "
        "writer_doctor_prefix_cache_layout=true groq_cache_authoritative_after_warm=true "
        "groq_model_pool=gpt-oss-20b->gpt-oss-120b->qwen3.8-27b "
        f"openrouter_preflight_blocked={str(_OPENROUTER_STRUCTURAL_BLOCKED).lower()}"
    )

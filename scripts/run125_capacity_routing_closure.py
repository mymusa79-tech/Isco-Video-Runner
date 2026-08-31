from __future__ import annotations

import inspect
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


def _mark_cache_warm_from_observed_usage(provider_name: str, result: str) -> bool:
    """Trust Groq cache accounting only after the response proves cached tokens.

    Run132 showed that a successful request is not evidence of a prompt-cache hit. A
    false warm marker let the next Writer request bypass a known-insufficient TPM
    window. Groq exposes the authoritative cache evidence in
    usage.prompt_tokens_details.cached_tokens, already captured in response metadata.
    """
    if not str(provider_name).startswith("groq") or result != "success":
        return False
    group = _cache_group(router._CURRENT_REQUEST_META.get("response_contract"))
    model_name = _active_groq_model()
    cached_tokens = router._last_call_response_meta.get("cached_tokens")
    if (
        group
        and model_name in _CACHEABLE_GROQ_MODELS
        and isinstance(cached_tokens, (int, float))
        and not isinstance(cached_tokens, bool)
        and cached_tokens > 0
    ):
        _WARM_CACHE_GROUPS.add((model_name, group))
        return True
    return False


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
    # Capacity evidence is model-scoped. Never clear another model's state merely
    # because routing moved; the new model starts from its own persisted/observed state.
    router._last_call_rate_limit_headers.clear()
    router._last_call_response_meta.clear()
    print(f"Run125 Groq model failover: {previous} -> {current} reason={reason}")
    return True


def _groq_model_call(prompt: str, model_name: str) -> dict:
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
            f"{marker} model={model_name} contract={request_capacity['contract']} "
            f"estimated_total={request_capacity['estimated_request_tokens']} "
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
        capacity.observe_groq_response(
            response,
            model_name,
            required_tokens=request_capacity["estimated_request_tokens"],
        )
        router._last_call_rate_limit_headers.update(router._extract_rate_limit_headers(response.headers))
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


def _provider_preflight_path() -> Path | None:
    explicit = str(os.environ.get("ISCO_PROVIDER_PREFLIGHT_PATH") or "").strip()
    if explicit:
        return Path(explicit)
    temp = str(os.environ.get("RUNNER_TEMP") or "").strip()
    return Path(temp) / "provider-preflight.json" if temp else None


def _openrouter_preflight_check(path: Path | None = None) -> dict | None:
    target = path or _provider_preflight_path()
    if target is None or not target.is_file():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    for check in data.get("checks") or []:
        if not isinstance(check, dict):
            continue
        if str(check.get("provider") or "").strip().lower() != "openrouter":
            continue
        return check
    return None


def openrouter_preflight_blocked(path: Path | None = None) -> bool:
    check = _openrouter_preflight_check(path)
    if check is None:
        return False
    return str(check.get("status") or "").strip().lower() == "block"


def openrouter_preflight_block_detail(path: Path | None = None) -> str:
    """The real preflight failure reason (e.g. provider_preflight.check_openrouter's
    RuntimeError text), so a run that never even attempts OpenRouter still logs WHY -
    instead of the opaque, undifferentiated marker this closure used before."""
    check = _openrouter_preflight_check(path)
    if check is None:
        return "preflight_check_result_unavailable"
    detail = str(check.get("detail") or "").strip()
    return detail[:200] if detail else "preflight_marked_block_without_detail"


def _assert_pacing_contract(callable_obj, *, label: str) -> None:
    """Fail during installer composition, before a real provider call can expose drift."""
    try:
        inspect.signature(callable_obj, follow_wrapped=False).bind(
            {"estimated_request_tokens": 1},
            model_name=_active_groq_model(),
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"RUNTIME_CALL_CONTRACT_MISMATCH target={label} expected=model_aware_pacing detail={exc}"
        ) from exc


def install_run125_capacity_routing_closure() -> None:
    global _INSTALLED
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

    # 2) Expose cache-hit telemetry and warm a family only after Groq itself proves a
    # positive cached-token count. A successful request alone is not cache evidence.
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
        _mark_cache_warm_from_observed_usage(provider_name, result)
        original_record(provider_name, result, **kwargs)

    router._record_attempt = cache_record

    original_pacing = capacity._proactive_groq_pacing
    _assert_pacing_contract(original_pacing, label="pre_run125_capacity._proactive_groq_pacing")

    def cache_aware_pacing(
        request_capacity: dict,
        model_name: str = capacity._DEFAULT_GROQ_MODEL,
    ) -> float:
        model = str(model_name or _active_groq_model()).strip() or _active_groq_model()
        group = _cache_group(request_capacity.get("contract"))
        required = int(request_capacity.get("estimated_request_tokens") or 0)
        decision = capacity.groq_admission_decision(model, required)

        # Cached-token accounting may make a full local prompt estimate larger than the
        # remaining minute window. Bypass WAIT only after this exact model/family has a
        # provider-reported positive cached_tokens observation. Impossible or unavailable
        # states still go through the canonical pacing owner and fail fast.
        if (
            decision["action"] == "wait"
            and group
            and (model, group) in _WARM_CACHE_GROUPS
        ):
            print(
                "Run125 Groq cache-aware probe: "
                f"model={model} group={group} required_full_estimate={required} "
                f"remaining_window={decision['remaining_tokens']} "
                "action=provider_authoritative_cache_accounting"
            )
            return 0.0
        return original_pacing(request_capacity, model_name=model)

    _assert_pacing_contract(cache_aware_pacing, label="run125_cache_aware_pacing")
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
    #
    # The two conditions are kept as DISTINCT, honestly-labeled reasons rather than one
    # opaque "OPENROUTER_NO_PROVIDER_AVAILABLE" string. That marker previously collapsed
    # three different things into one label with no way to tell them apart after the
    # fact: (a) preflight found a real account/model problem before any call this run,
    # (b) this run's own first output-heavy OpenRouter call truncated (an output-shape
    # issue, not a "no provider" issue) and the closure is deliberately not repeating
    # that failure on every later shard, and (c) OpenRouter's live API genuinely
    # returning "no providers/endpoints available" for the requested model (handled by
    # classify_provider_failure's own OpenRouter-error-text matching, not this closure).
    # Real production telemetry showing this label on most failed runs was almost always
    # case (a) or (b), which are Runner-local decisions, not an external outage - and
    # case (a) already carries the real preflight failure detail (see
    # openrouter_preflight_block_detail), so it is no longer silently discarded.
    _OPENROUTER_BLOCK_REASON: str | None = (
        "preflight_blocked: " + openrouter_preflight_block_detail() if openrouter_preflight_blocked() else None
    )
    original_openrouter = router._openrouter_structured_request

    def bounded_openrouter(prompt: str, contract: tuple[str, dict]) -> dict:
        nonlocal _OPENROUTER_BLOCK_REASON
        if _OPENROUTER_BLOCK_REASON is not None:
            raise RuntimeError(f"OPENROUTER_UNAVAILABLE_THIS_RUN reason={_OPENROUTER_BLOCK_REASON}")
        try:
            return original_openrouter(prompt, contract)
        except Exception as exc:
            lower = str(exc).lower()
            if "premature_response" in lower or "finish_reason=length" in lower:
                _OPENROUTER_BLOCK_REASON = (
                    "structural_truncation_circuit: first output-heavy call this run truncated "
                    "(finish_reason=length/premature_response) - not a provider-availability failure, "
                    "blocking further OpenRouter attempts this run to avoid repeating it on every shard"
                )
                print("Run125 OpenRouter structural circuit armed after first truncated output")
            raise

    router._openrouter_structured_request = bounded_openrouter

    _INSTALLED = True
    print(
        "Run125 capacity routing closure installed: "
        "writer_doctor_prefix_cache_layout=true groq_cache_authoritative_after_proven_hit=true "
        "groq_model_scoped_contract=true "
        "groq_model_pool=gpt-oss-20b->gpt-oss-120b->qwen3.8-27b "
        f"openrouter_blocked_this_run={str(_OPENROUTER_BLOCK_REASON is not None).lower()}"
    )

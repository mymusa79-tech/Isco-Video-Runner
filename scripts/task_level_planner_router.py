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

# Floor on the gap between two calls to the SAME provider, not a global throttle.
MIN_PROVIDER_CALL_INTERVAL_SECONDS = 1.5

# The Runner is the only owner of client-side transient retries. Provider SDK retries
# are disabled/avoided, while OpenRouter remains responsible for provider/model failover
# *inside* one OpenRouter request. One provider gets at most original + one transient
# retry before this router moves on.
TRANSIENT_PROVIDER_MAX_ATTEMPTS = 2
TRANSIENT_RETRY_BASE_SECONDS = 1.0
TRANSIENT_RETRY_JITTER_SECONDS = 0.5
TRANSIENT_PROVIDER_COOLDOWN_SECONDS = 30.0
_TRANSIENT_RESULTS = frozenset({"server_error", "timeout", "network_error"})

# Worst physical call count for one routed planning subtask:
# Gemini (2 transient attempts) + Groq (2) + OpenRouter (2 outer transient attempts,
# each of which can make one bounded invalid-JSON repair request) = 8. OpenRouter's
# internal provider/model fallbacks remain one HTTP request from BudgetLedger's view.
PLANNING_SUBTASK_MAX_PROVIDER_ATTEMPTS = 8

# Which providers actually produced planning output for this run, in first-use order.
_USED_PROVIDERS: list[str] = []

# Router telemetry is deliberately separate from BudgetLedger. It includes local
# circuit/cooldown skips for diagnosis. BudgetLedger counts only provider callables
# that were actually invoked.
_TELEMETRY: list[dict] = []

# Groq response rate-limit headers captured for router telemetry.
_last_call_rate_limit_headers: dict = {}


def _normalize_provider_name(name: str) -> str:
    return "openrouter" if name.startswith("openrouter") else name


def _record_provider_used(name: str) -> None:
    normalized = _normalize_provider_name(name)
    if normalized not in _USED_PROVIDERS:
        _USED_PROVIDERS.append(normalized)


def get_used_providers() -> list[str]:
    """Providers that actually produced planning output for the current/last run."""
    return list(_USED_PROVIDERS)


def get_telemetry() -> list[dict]:
    """Every outer router interaction recorded so far in the current/last run."""
    return list(_TELEMETRY)


def _extract_rate_limit_headers(headers) -> dict:
    return {
        "retry_after": headers.get("Retry-After"),
        "remaining_requests": headers.get("X-RateLimit-Remaining-Requests"),
        "remaining_tokens": headers.get("X-RateLimit-Remaining-Tokens"),
    }


def _record_attempt(
    provider_name: str,
    result: str,
    *,
    error_detail: str | None = None,
    duration_seconds: float | None = None,
) -> None:
    headers = dict(_last_call_rate_limit_headers)
    _last_call_rate_limit_headers.clear()
    _TELEMETRY.append(
        {
            "provider": provider_name,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "result": result,
            "error_detail": error_detail,
            "duration_seconds": duration_seconds,
            "retry_after": headers.get("retry_after"),
            "remaining_requests": headers.get("remaining_requests"),
            "remaining_tokens": headers.get("remaining_tokens"),
        }
    )


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
        raise RuntimeError(
            f"AI budget authorization denied for task {active.spec.task_id}; provider call blocked"
        )
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
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


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
            "editorial_thesis",
            "viewer_starting_belief",
            "hidden_assumption",
            "editorial_turn",
            "stakes",
            "viewer_promise",
            "evidence_boundaries",
            "earned_payoff",
        ],
    )
    brief = _strict_object(
        {
            "id": {"type": "string"},
            "purpose": {"type": "string"},
            "visual_query": {"type": "string"},
            "on_screen_text": {"type": "string"},
            "emotion": {"type": "string"},
            "expected_seconds": {"type": "number"},
        },
        ["id", "purpose", "visual_query", "on_screen_text", "emotion", "expected_seconds"],
    )
    return _strict_object(
        {
            "pillar": {"type": "string"},
            "hook": {"type": "string"},
            "title_options": _string_array(min_items=3, max_items=3),
            "thumbnail_concepts": _string_array(min_items=3, max_items=3),
            "cta": {"type": "string"},
            "closing_payoff": {"type": "string"},
            "narrative_format": {"type": "string"},
            "opener_variant": {"type": "string"},
            "closer_variant": {"type": "string"},
            "transition_variants": _string_array(min_items=3, max_items=3),
            "editorial_intent": editorial_intent,
            "section_briefs": {
                "type": "array",
                "items": brief,
                "minItems": expected,
                "maxItems": expected,
            },
        },
        [
            "pillar",
            "hook",
            "title_options",
            "thumbnail_concepts",
            "cta",
            "closing_payoff",
            "narrative_format",
            "opener_variant",
            "closer_variant",
            "transition_variants",
            "editorial_intent",
            "section_briefs",
        ],
    )


def _structured_schema_for_prompt(prompt: str) -> tuple[str, dict] | None:
    """Return the strict schema for known planner contracts; unknown tasks stay JSON mode."""
    expected = _expected_sections(prompt)
    if expected is not None and "section_briefs" in prompt:
        return "editorial_outline", _outline_response_schema(expected)

    exact_match = re.search(r"with EXACTLY\s*(\d+)\s+entries", prompt, flags=re.I)
    exact_count = int(exact_match.group(1)) if exact_match else None
    if exact_count is not None and '"sections"' in prompt:
        item = _strict_object(
            {
                "id": {"type": "string"},
                "narration": {"type": "string"},
                "key_point": {"type": "string"},
            },
            ["id", "narration", "key_point"],
        )
        return "full_script", _strict_object(
            {
                "sections": {
                    "type": "array",
                    "items": item,
                    "minItems": exact_count,
                    "maxItems": exact_count,
                }
            },
            ["sections"],
        )

    if exact_count is not None and '"additions"' in prompt:
        item = _strict_object(
            {"id": {"type": "string"}, "append_text": {"type": "string"}},
            ["id", "append_text"],
        )
        return "append_only_repair", _strict_object(
            {
                "additions": {
                    "type": "array",
                    "items": item,
                    "minItems": exact_count,
                    "maxItems": exact_count,
                }
            },
            ["additions"],
        )

    if 'Return ONLY JSON: {"narration"' in prompt:
        return "section_repair", _strict_object(
            {"narration": {"type": "string"}},
            ["narration"],
        )
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
    """Make dialogue output machine-routable without adding another model call."""
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


def _groq_call(prompt: str) -> dict:
    token = _read_secret_file("GROQ_API_KEY_FILE")

    def do_request() -> dict:
        response = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
            json={
                "model": "openai/gpt-oss-20b",
                "messages": [
                    {
                        "role": "user",
                        "content": prompt + "\nReturn ONLY one complete valid JSON object. No markdown.",
                    }
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.15,
                "max_completion_tokens": 2600,
            },
            timeout=90,
        )
        _last_call_rate_limit_headers.update(_extract_rate_limit_headers(response.headers))
        if response.status_code == 429:
            raise RuntimeError("RATE_LIMIT_429")
        if not response.ok:
            raise RuntimeError(f"Groq HTTP {response.status_code}")
        body = response.json()
        choices = body.get("choices") or []
        if not choices:
            raise RuntimeError("Groq returned no choices")
        return _parse_json(choices[0]["message"]["content"])

    return _budgeted_provider_call("groq", "openai/gpt-oss-20b", do_request)


_OPENROUTER_REPAIR_SUFFIX = "\n\nأعد الرد بصيغة JSON صالحة فقط، بدون أي نص إضافي قبله أو بعده."
_OPENROUTER_FALLBACK_MODELS = ("openai/gpt-oss-20b:free",)


def _openrouter_call_with_repair(
    prompt: str,
    model: str,
    provider_name: str = "openrouter",
    *,
    response_contract: tuple[str, dict] | None = None,
) -> dict:
    """One OpenRouter request chain plus one bounded syntax repair when necessary.

    The no-contract branch deliberately preserves the old two-argument adapter call.
    That keeps legacy/unknown JSON-only planner tasks compatible during the rollout;
    all known production planning contracts use the strict schema branch below.
    """
    if response_contract is None:
        kwargs = {"model": model}
    else:
        schema_name, response_schema = response_contract
        kwargs = {
            "model": model,
            "fallback_models": _OPENROUTER_FALLBACK_MODELS,
            "response_schema": response_schema,
            "schema_name": schema_name,
        }

    try:
        return _budgeted_provider_call(
            provider_name,
            model,
            openrouter_json_text,
            prompt,
            **kwargs,
        )
    except RuntimeError as exc:
        if "invalid JSON" not in str(exc):
            raise
        return _budgeted_provider_call(
            provider_name,
            model,
            openrouter_json_text,
            prompt + _OPENROUTER_REPAIR_SUFFIX,
            **kwargs,
        )


def _retry_delay_seconds(provider_name: str, retry_index: int) -> float:
    """Deterministic bounded jitter keeps tests reproducible and concurrent runs de-synced."""
    digest = hashlib.sha256(f"{provider_name}:{retry_index}".encode("utf-8")).digest()
    fraction = int.from_bytes(digest[:2], "big") / 65535.0
    exponential = TRANSIENT_RETRY_BASE_SECONDS * (2 ** retry_index)
    return exponential + (fraction * TRANSIENT_RETRY_JITTER_SECONDS)


def install_router() -> None:
    checkpoint = _load_checkpoint()
    responses = checkpoint.setdefault("responses", {})
    cooldown: set[str] = set()
    transient_cooldown_until: dict[str, float] = {}
    last_call_at: dict[str, float] = {}
    planning_subtask_sequence = 0
    _USED_PROVIDERS.clear()
    _TELEMETRY.clear()
    gemini_key = _read_secret_file("GEMINI_API_KEY_FILE")

    providers = [
        (
            "gemini",
            lambda prompt, model: _budgeted_provider_call(
                "gemini", model, gemini_json_text, gemini_key, prompt, model=model
            ),
        ),
        ("groq", lambda prompt, model: _groq_call(prompt)),
        (
            "openrouter",
            lambda prompt, model: _openrouter_call_with_repair(
                prompt,
                "openrouter/free",
                "openrouter",
                response_contract=_structured_schema_for_prompt(prompt),
            ),
        ),
    ]

    def task_router(_api_key, prompt, model="gemini-2.5-flash"):
        nonlocal planning_subtask_sequence
        prompt = _enrich_dialogue_prompt(prompt)
        prompt = with_channel_persona(prompt)
        cache_key = hashlib.sha256((model + "\n" + prompt).encode("utf-8")).hexdigest()
        cached = responses.get(cache_key)
        if isinstance(cached, dict):
            print("Planning checkpoint hit")
            return cached

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
                        raw = provider(prompt, model)
                        data = _normalize_outline(_parse_json(raw), prompt)
                        responses[cache_key] = data
                        checkpoint["last_provider"] = name
                        _save_checkpoint(checkpoint)
                        _record_provider_used(name)
                        _record_attempt(
                            name,
                            "success",
                            duration_seconds=time.monotonic() - last_call_at[name],
                        )
                        print(f"Planning subtask provider selected: {name}")
                        return data
                    except Exception as exc:
                        detail = str(exc).replace("\n", " ")[:220]
                        failure = classify_provider_failure(name, exc)
                        _record_attempt(
                            name,
                            failure.telemetry_result,
                            error_detail=detail,
                            duration_seconds=time.monotonic() - last_call_at[name],
                        )

                        retryable = failure.telemetry_result in _TRANSIENT_RESULTS
                        has_retry = provider_attempt + 1 < TRANSIENT_PROVIDER_MAX_ATTEMPTS
                        if retryable and has_retry:
                            delay = _retry_delay_seconds(name, provider_attempt)
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
                            transient_cooldown_until[name] = (
                                time.monotonic() + TRANSIENT_PROVIDER_COOLDOWN_SECONDS
                            )
                            print(
                                "Planning provider transient cooldown: "
                                f"{name} seconds={TRANSIENT_PROVIDER_COOLDOWN_SECONDS:g}"
                            )
                        else:
                            print(f"Planning subtask failed safely: {name}:{detail}")
                        break

            raise RuntimeError(
                "All free providers failed for planning subtask: " + " | ".join(failures)
            )

        active = get_active_budget_task()
        if active is None or active.spec.kind != "OUTLINE_PLAN":
            return run_provider_loop()

        planning_subtask_sequence += 1
        child = TaskSpec(
            task_id=(
                f"{active.spec.task_id}_{active.spec.priority.name}_"
                f"SUBTASK_{planning_subtask_sequence:03d}"
            ),
            kind="PLANNING_SUBTASK",
            priority=active.spec.priority,
            capability=active.spec.capability,
            max_provider_attempts=PLANNING_SUBTASK_MAX_PROVIDER_ATTEMPTS,
            schema_repair_allowed=active.spec.schema_repair_allowed,
            local_fallback=False,
            semantic_block_is_final=False,
        )
        with budget_task_scope(
            active.ledger,
            child,
            requested_model=active.requested_model,
        ):
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

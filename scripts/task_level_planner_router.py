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
from isco_video_agent.ai_budget import AttemptOutcome, get_active_budget_task
from isco_video_agent.providers.gemini import json_text as gemini_json_text
from isco_video_agent.providers.gemini import with_channel_persona
from isco_video_agent.providers.openrouter import json_text as openrouter_json_text


CACHE_PATH = Path("state/planning-checkpoint.json")
CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)

# Floor on the gap between two calls to the SAME provider, not a global throttle.
MIN_PROVIDER_CALL_INTERVAL_SECONDS = 1.5

# Which providers actually produced planning output for this run, in first-use order.
_USED_PROVIDERS: list[str] = []

# Router telemetry is deliberately separate from BudgetLedger. It includes local
# circuit-open skips for diagnosis; BudgetLedger provider attempts count only provider
# callables that are actually invoked. OpenRouter's JSON repair therefore contributes
# two BudgetLedger attempts but remains one outer router interaction in this telemetry.
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


def _classify_failure(detail: str) -> str:
    lower = detail.lower()
    if "429" in detail or "quota" in lower:
        return "429"
    if "invalid json" in lower:
        return "invalid_json"
    if "premature" in lower:
        return "premature_response"
    return "other"


def _classify_budget_outcome(exc: Exception) -> AttemptOutcome:
    detail = str(exc).lower()
    if "429" in str(exc) or "quota" in detail or "rate limit" in detail:
        return AttemptOutcome.RATE_LIMITED
    if "invalid json" in detail or "complete json object" in detail:
        return AttemptOutcome.SCHEMA_INVALID
    if "premature" in detail:
        return AttemptOutcome.TRUNCATED
    if "timeout" in detail or "timed out" in detail:
        return AttemptOutcome.TIMEOUT
    if "connection" in detail or "network" in detail:
        return AttemptOutcome.NETWORK_ERROR
    return AttemptOutcome.OTHER


def _record_attempt(provider_name: str, result: str, *, error_detail: str | None = None, duration_seconds: float | None = None) -> None:
    headers = dict(_last_call_rate_limit_headers)
    _last_call_rate_limit_headers.clear()
    _TELEMETRY.append({
        "provider": provider_name,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "result": result,
        "error_detail": error_detail,
        "duration_seconds": duration_seconds,
        "retry_after": headers.get("retry_after"),
        "remaining_requests": headers.get("remaining_requests"),
        "remaining_tokens": headers.get("remaining_tokens"),
    })


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
    """Account for exactly one provider callable invocation.

    Authorization is enforced at this individual provider-call boundary. When the
    active BudgetLedger refuses the next attempt, the provider callable is never
    invoked and no attempt record is manufactured for a request that did not happen.
    For OpenRouter repair, each json_text invocation still has its own authorization.

    With no active BudgetLedger scope this is a transparent direct call: no authorize,
    timing, or bookkeeping side effects are introduced into the legacy Runner path.
    """
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
        _record_budget_attempt(
            provider_name,
            resolved_model,
            _classify_budget_outcome(exc),
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
                "messages": [{"role": "user", "content": prompt + "\nReturn ONLY one complete valid JSON object. No markdown."}],
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

    # Secret-file reading is local setup; only the provider request/response work is
    # inside the provider-attempt accounting boundary.
    return _budgeted_provider_call("groq", "openai/gpt-oss-20b", do_request)


_OPENROUTER_REPAIR_SUFFIX = "\n\nأعد الرد بصيغة JSON صالحة فقط، بدون أي نص إضافي قبله أو بعده."


def _openrouter_call_with_repair(prompt: str, model: str, provider_name: str | None = None) -> dict:
    """Exactly one JSON repair request after an invalid-JSON first response.

    Each openrouter_json_text() invocation is separately budgeted. Therefore an
    invalid first response followed by a successful repair is two real provider
    attempts in BudgetLedger, not one outer router interaction. provider_name remains
    optional for backwards compatibility with the pre-accounting two-argument helper.
    """
    if provider_name is None:
        provider_name = (
            "openrouter-free-router"
            if model == "openrouter/free"
            else "openrouter-gpt-oss-free"
        )
    try:
        return _budgeted_provider_call(
            provider_name,
            model,
            openrouter_json_text,
            prompt,
            model=model,
        )
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


def install_router() -> None:
    checkpoint = _load_checkpoint()
    responses = checkpoint.setdefault("responses", {})
    cooldown: set[str] = set()
    last_call_at: dict[str, float] = {}
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
            "openrouter-free-router",
            lambda prompt, model: _openrouter_call_with_repair(
                prompt, "openrouter/free", "openrouter-free-router"
            ),
        ),
        (
            "openrouter-gpt-oss-free",
            lambda prompt, model: _openrouter_call_with_repair(
                prompt, "openai/gpt-oss-20b:free", "openrouter-gpt-oss-free"
            ),
        ),
    ]

    def task_router(_api_key, prompt, model="gemini-2.5-flash"):
        prompt = _enrich_dialogue_prompt(prompt)
        prompt = with_channel_persona(prompt)
        cache_key = hashlib.sha256((model + "\n" + prompt).encode("utf-8")).hexdigest()
        cached = responses.get(cache_key)
        if isinstance(cached, dict):
            # Deliberately before the provider loop: no provider callable, authorize,
            # or BudgetLedger record occurs for a checkpoint hit.
            print("Planning checkpoint hit")
            return cached

        failures: list[str] = []
        for name, provider in providers:
            if name in cooldown:
                # Diagnostic telemetry only. No BudgetLedger attempt: no provider
                # callable/network request is made.
                _record_attempt(name, "circuit-open")
                continue
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
                _record_attempt(name, "success", duration_seconds=time.monotonic() - last_call_at[name])
                print(f"Planning subtask provider selected: {name}")
                return data
            except Exception as exc:
                detail = str(exc).replace("\n", " ")[:220]
                failures.append(f"{name}:{detail}")
                _record_attempt(name, _classify_failure(detail), error_detail=detail, duration_seconds=time.monotonic() - last_call_at[name])
                if "429" in detail or "quota" in detail.lower():
                    cooldown.add(name)
                    print(f"Planning provider circuit-open for this run: {name}")
                else:
                    print(f"Planning subtask failed safely: {name}:{detail}")

        raise RuntimeError("All free providers failed for planning subtask: " + " | ".join(failures))

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
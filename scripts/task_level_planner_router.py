from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

import requests

import isco_video_agent.orchestrator as orchestrator
import isco_video_agent.resilient_planner as staged
from isco_video_agent.providers.gemini import json_text as gemini_json_text
from isco_video_agent.providers.gemini import with_channel_persona
from isco_video_agent.providers.openrouter import json_text as openrouter_json_text


CACHE_PATH = Path("state/planning-checkpoint.json")
CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)

# A film's 8 sections mean many back-to-back planning subtasks in one run, with no
# gap between them otherwise (diagnosed after run 31870521024: 17 consecutive Gemini
# calls with zero delay tripped its free-tier per-minute rate limit within a single
# run, not from quota exhausted across the day's earlier attempts). This is a floor on
# the gap between two calls to the SAME provider, not a global throttle.
MIN_PROVIDER_CALL_INTERVAL_SECONDS = 1.5

# Which providers actually produced the plan used for this run's video, in the order
# first used. Reset at the top of install_router() (one production run per script
# invocation) and read back by run_v3_voice.py after produce() returns to tag
# plan.json/quality-final.json with plan_source. A cache hit doesn't append here: the
# original call that populated the cache already recorded its provider.
_USED_PROVIDERS: list[str] = []


def _normalize_provider_name(name: str) -> str:
    return "openrouter" if name.startswith("openrouter") else name


def _record_provider_used(name: str) -> None:
    normalized = _normalize_provider_name(name)
    if normalized not in _USED_PROVIDERS:
        _USED_PROVIDERS.append(normalized)


def get_used_providers() -> list[str]:
    """Providers that actually produced planning output for the current/last run."""
    return list(_USED_PROVIDERS)


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
    if response.status_code == 429:
        raise RuntimeError("RATE_LIMIT_429")
    if not response.ok:
        raise RuntimeError(f"Groq HTTP {response.status_code}")
    body = response.json()
    choices = body.get("choices") or []
    if not choices:
        raise RuntimeError("Groq returned no choices")
    return _parse_json(choices[0]["message"]["content"])


_OPENROUTER_REPAIR_SUFFIX = "\n\nأعد الرد بصيغة JSON صالحة فقط، بدون أي نص إضافي قبله أو بعده."


def _openrouter_call_with_repair(prompt: str, model: str) -> dict:
    """OpenRouter's free-tier models are the observed failure mode here (never 429 -
    always a malformed/non-JSON completion body from the model itself), so retrying
    the exact same request rarely helps; asking the model to reformat its own output
    sometimes does. Exactly one repair attempt: if it also fails, for any reason, that
    exception propagates to task_router()'s normal failover to the next provider -
    no further attempts, no loop."""
    try:
        return openrouter_json_text(prompt, model=model)
    except RuntimeError as exc:
        if "invalid JSON" not in str(exc):
            raise
        return openrouter_json_text(prompt + _OPENROUTER_REPAIR_SUFFIX, model=model)


def install_router() -> None:
    checkpoint = _load_checkpoint()
    responses = checkpoint.setdefault("responses", {})
    cooldown: set[str] = set()
    last_call_at: dict[str, float] = {}
    _USED_PROVIDERS.clear()
    gemini_key = _read_secret_file("GEMINI_API_KEY_FILE")

    providers = [
        ("gemini", lambda prompt, model: gemini_json_text(gemini_key, prompt, model=model)),
        ("groq", lambda prompt, model: _groq_call(prompt)),
        ("openrouter-free-router", lambda prompt, model: _openrouter_call_with_repair(prompt, "openrouter/free")),
        ("openrouter-gpt-oss-free", lambda prompt, model: _openrouter_call_with_repair(prompt, "openai/gpt-oss-20b:free")),
    ]

    def task_router(_api_key, prompt, model="gemini-2.5-flash"):
        prompt = _enrich_dialogue_prompt(prompt)
        # Apply the channel identity injection once here, before the provider loop, so it
        # survives a fallback away from Gemini instead of only ever running on the Gemini
        # path inside gemini.json_text() below. with_channel_persona() is idempotent (its
        # own "<CHANNEL_PERSONA>" guard), so the "gemini" provider entry calling the real
        # gemini_json_text() -> json_text() -> with_channel_persona() again on this already-
        # enriched prompt is a safe no-op, not a double injection.
        prompt = with_channel_persona(prompt)
        cache_key = hashlib.sha256((model + "\n" + prompt).encode("utf-8")).hexdigest()
        cached = responses.get(cache_key)
        if isinstance(cached, dict):
            print("Planning checkpoint hit")
            return cached

        failures: list[str] = []
        for name, provider in providers:
            if name in cooldown:
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
                print(f"Planning subtask provider selected: {name}")
                return data
            except Exception as exc:
                detail = str(exc).replace("\n", " ")[:220]
                failures.append(f"{name}:{detail}")
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

    # Marker orchestrator.py's _verify_resilient_router_installed() checks directly on
    # this callable at call time (not a side-channel "install_router() ran" flag, which
    # could go stale if build_plan were ever reassigned afterward). Must be set before
    # the assignment below so the exact object orchestrator.build_plan ends up bound to
    # carries it.
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

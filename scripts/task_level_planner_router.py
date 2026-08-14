from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

import requests

import isco_video_agent.orchestrator as orchestrator
import isco_video_agent.resilient_planner as staged
from isco_video_agent.providers.gemini import json_text as gemini_json_text
from isco_video_agent.providers.openrouter import json_text as openrouter_json_text


CACHE_PATH = Path("state/planning-checkpoint.json")
CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)


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


def install_router() -> None:
    checkpoint = _load_checkpoint()
    responses = checkpoint.setdefault("responses", {})
    cooldown: set[str] = set()
    gemini_key = _read_secret_file("GEMINI_API_KEY_FILE")

    providers = [
        ("gemini", lambda prompt, model: gemini_json_text(gemini_key, prompt, model=model)),
        ("groq", lambda prompt, model: _groq_call(prompt)),
        ("openrouter-free-router", lambda prompt, model: openrouter_json_text(prompt, model="openrouter/free")),
        ("openrouter-gpt-oss-free", lambda prompt, model: openrouter_json_text(prompt, model="openai/gpt-oss-20b:free")),
    ]

    def task_router(_api_key, prompt, model="gemini-2.5-flash"):
        prompt = _enrich_dialogue_prompt(prompt)
        cache_key = hashlib.sha256((model + "\n" + prompt).encode("utf-8")).hexdigest()
        cached = responses.get(cache_key)
        if isinstance(cached, dict):
            print("Planning checkpoint hit")
            return cached

        failures: list[str] = []
        for name, provider in providers:
            if name in cooldown:
                continue
            try:
                raw = provider(prompt, model)
                data = _normalize_outline(_parse_json(raw), prompt)
                responses[cache_key] = data
                checkpoint["last_provider"] = name
                _save_checkpoint(checkpoint)
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
            print("Dialogue voice mode selected: questioner=Kore responder=Gacrux")
        else:
            os.environ.pop("ISCO_DIALOGUE_QA", None)
        return plan

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

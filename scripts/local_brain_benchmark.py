from __future__ import annotations

import json
import os
import re
import resource
import sys
import time
import urllib.request
from pathlib import Path

ENDPOINT = os.environ.get("LOCAL_LLM_ENDPOINT", "http://127.0.0.1:8080/v1/chat/completions")
MODEL = os.environ.get("LOCAL_LLM_MODEL", "local-qwen3-4b")
OUT = Path(os.environ.get("LOCAL_BRAIN_REPORT", "local-brain-report.json"))
TIME_BUDGET_S = int(os.environ.get("LOCAL_BRAIN_TIME_BUDGET_S", "420"))
MAX_RSS_MB = int(os.environ.get("LOCAL_BRAIN_MAX_RSS_MB", "12288"))
MIN_ARABIC_RATIO = float(os.environ.get("LOCAL_BRAIN_MIN_ARABIC_RATIO", "0.55"))

ARCHITECT_SCHEMA = {
    "type": "object",
    "properties": {
        "hook_strategy": {"type": "string"},
        "core_promise": {"type": "string"},
        "sections": {
            "type": "array",
            "minItems": 8,
            "maxItems": 8,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "purpose": {"type": "string"},
                    "key_point": {"type": "string"},
                    "example": {"type": "string"},
                    "transition": {"type": "string"},
                    "target_words": {"type": "integer", "minimum": 90, "maximum": 170}
                },
                "required": ["id", "purpose", "key_point", "example", "transition", "target_words"],
                "additionalProperties": False
            }
        }
    },
    "required": ["hook_strategy", "core_promise", "sections"],
    "additionalProperties": False
}

SECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "narration": {"type": "string"},
        "visual_query": {"type": "string"},
        "on_screen_text": {"type": "string"},
        "emotion": {"type": "string"}
    },
    "required": ["narration", "visual_query", "on_screen_text", "emotion"],
    "additionalProperties": False
}


def _call(prompt: str, schema: dict, max_tokens: int) -> tuple[dict, float]:
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "You are a disciplined Arabic production editor. Return only the requested structured data. Do not use thinking/reasoning text."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.45,
        "top_p": 0.9,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_schema", "json_schema": {"name": "isco_contract", "strict": True, "schema": schema}},
    }
    req = urllib.request.Request(ENDPOINT, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    started = time.monotonic()
    with urllib.request.urlopen(req, timeout=min(TIME_BUDGET_S, 600)) as response:
        body = json.loads(response.read().decode())
    elapsed = time.monotonic() - started
    text = body["choices"][0]["message"]["content"]
    return json.loads(text), elapsed


def _arabic_ratio(text: str) -> float:
    letters = re.findall(r"[^\W\d_]", text, flags=re.UNICODE)
    if not letters:
        return 0.0
    arabic = re.findall(r"[\u0600-\u06FF]", "".join(letters))
    return len(arabic) / len(letters)


def _validate_architect(data: dict) -> list[str]:
    errors: list[str] = []
    sections = data.get("sections") or []
    if len(sections) != 8:
        errors.append(f"section_count={len(sections)}")
    ids = [str(x.get("id", "")) for x in sections if isinstance(x, dict)]
    if len(set(ids)) != len(ids):
        errors.append("duplicate_section_ids")
    if not data.get("hook_strategy") or not data.get("core_promise"):
        errors.append("missing_hook_or_promise")
    return errors


def _validate_section(data: dict) -> list[str]:
    errors: list[str] = []
    narration = str(data.get("narration", "")).strip()
    visual = str(data.get("visual_query", "")).strip()
    words = len(re.findall(r"\S+", narration))
    if not 80 <= words <= 180:
        errors.append(f"section_words={words}")
    ratio = _arabic_ratio(narration)
    if ratio < MIN_ARABIC_RATIO:
        errors.append(f"arabic_ratio={ratio:.3f}")
    if not re.search(r"[A-Za-z]", visual) or re.search(r"[\u0600-\u06FF]", visual):
        errors.append("visual_query_not_english")
    return errors


def main() -> int:
    topic = os.environ.get("LOCAL_BRAIN_TOPIC", "كيف تنهض بعد أن سقطت كثيرًا؟").strip()
    started = time.monotonic()
    report: dict = {"topic": topic, "model": MODEL, "endpoint": ENDPOINT, "budgets": {"seconds": TIME_BUDGET_S, "max_rss_mb": MAX_RSS_MB}}
    try:
        architect_prompt = f"""Build an eight-part production architecture for an Arabic YouTube film about: {topic}\nWrite natural Modern Standard Arabic. Each section must advance a distinct idea and avoid generic motivational filler. Do not invent medical or religious claims. The eight target word counts should total roughly 900-1100 spoken Arabic words. Metadata only: do not write the full narration yet."""
        arch, t_arch = _call(architect_prompt, ARCHITECT_SCHEMA, 1800)
        arch_errors = _validate_architect(arch)
        report["architect"] = {"seconds": round(t_arch, 2), "errors": arch_errors, "output": arch}
        if arch_errors:
            raise RuntimeError("architect contract failed")

        first = arch["sections"][0]
        section_prompt = f"""Topic: {topic}\nCore promise: {arch['core_promise']}\nWrite only section {first['id']} from this production architecture. Purpose: {first['purpose']} Key point: {first['key_point']} Example: {first['example']} Transition: {first['transition']} Target about {first['target_words']} Arabic words. Use natural contemporary Modern Standard Arabic, concrete observations, varied sentences, no filler, no invented factual/religious claims. visual_query must be concise realistic English stock-footage search terms."""
        section, t_section = _call(section_prompt, SECTION_SCHEMA, 900)
        section_errors = _validate_section(section)
        report["section_sample"] = {"seconds": round(t_section, 2), "errors": section_errors, "output": section}
        if section_errors:
            raise RuntimeError("section contract failed")

        total = time.monotonic() - started
        rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        report["total_seconds"] = round(total, 2)
        report["python_max_rss_mb"] = round(rss_mb, 1)
        report["pass"] = total <= TIME_BUDGET_S and rss_mb <= MAX_RSS_MB
        if total > TIME_BUDGET_S:
            report.setdefault("errors", []).append("time_budget_exceeded")
        if rss_mb > MAX_RSS_MB:
            report.setdefault("errors", []).append("memory_budget_exceeded")
    except Exception as exc:
        report["pass"] = False
        report["failure"] = f"{type(exc).__name__}: {exc}"
        report["total_seconds"] = round(time.monotonic() - started, 2)

    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k not in {"architect", "section_sample"}}, ensure_ascii=False, indent=2))
    return 0 if report.get("pass") else 1


if __name__ == "__main__":
    sys.exit(main())

"""Isolated planning-quality probe: Cloudflare gpt-oss-120b vs Mistral.

Builds the real Short planning prompt, calls each provider once per topic and runs
the real local plan validator. No production run, no media, no stored state.
Cloudflare text calls are capped by providers.CLOUDFLARE_TEXT_MAX_CALLS_PER_RUN.
"""
from __future__ import annotations

import json
import os
import sys
import time

from clean_v2 import pipeline, providers

TOPICS = [
    "لماذا تؤجل المهام المهمة رغم أنك تعرف أنها مهمة؟",
    "لماذا تشعر بالتعب بعد يوم لم تفعل فيه شيئاً؟",
    "كيف يغيّر الروتين الصغير صباحك دون إرهاق؟",
]


def brief(topic: str) -> dict:
    return {
        "approved_by_user": True,
        "approved_topic": topic,
        "format": "short",
        "language": "ar",
        "audience": "Arabic-speaking adults",
        "editorial_intent": "نبرة هادئة وعملية وطبيعية.",
        "research_pack": [],
        "hard_constraints": ["No fabricated facts."],
    }


def attempt(name, call, prompt, b):
    started = time.time()
    out = {"provider": name}
    try:
        value = call(prompt)
        out["json_ok"] = isinstance(value, dict)
        pipeline._validate_plan_for_brief(value, b, enforce_visual_identity=False)
        out["validator"] = "pass"
    except Exception as exc:  # noqa: BLE001 - probe reports every failure class
        out.setdefault("json_ok", False)
        out["validator"] = "fail"
        out["error_type"] = type(exc).__name__
        out["error"] = str(exc)[:240]
        out["http_status"] = getattr(exc, "http_status", None)
    out["seconds"] = round(time.time() - started, 1)
    return out


def main() -> int:
    os.environ.setdefault("CLOUDFLARE_TEXT_MAX_CALLS_PER_RUN", "3")
    rows = []
    for topic in TOPICS:
        b = brief(topic)
        prompt = pipeline._planning_prompt(b)
        row = {"topic": topic, "prompt_utf8_bytes": len(prompt.encode("utf-8")), "results": []}
        row["results"].append(attempt("cloudflare_gpt_oss_120b", lambda p: providers._cloudflare_call(p, 8000), prompt, b))
        row["results"].append(attempt("mistral", lambda p: providers._mistral_call(p, 3000, "planning"), prompt, b))
        rows.append(row)
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    for index, row in enumerate(rows, 1):
        for res in row["results"]:
            line = json.dumps({"t": index, "bytes": row["prompt_utf8_bytes"], **res}, ensure_ascii=False)
            print(f"::notice title=probe {res['provider']}::{line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

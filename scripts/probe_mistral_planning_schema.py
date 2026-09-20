from __future__ import annotations

import hashlib
import json
from pathlib import Path

from clean_v2 import providers
from clean_v2.contracts import compute_brief_sha256, validate_plan
from clean_v2.pipeline import _planning_prompt

EXPECTED_BRIEF_SHA256 = "bcf8d3017ee8e18ee4808614c7c07731b0452a5ba6182e1183404f663078e129"


def _run_case(label: str, brief: dict) -> dict:
    prompt = _planning_prompt(brief)
    value = providers._mistral_call(prompt, 4000, "planning")
    normalized = validate_plan(value, brief)
    return {
        "label": label,
        "status": "contract_pass",
        "format": brief["format"],
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "top_level_keys": sorted(value.keys()),
        "sections_count": len(normalized["sections"]),
        "section_ids": [item["id"] for item in normalized["sections"]],
        "section_keys": [sorted(item.keys()) for item in normalized["sections"]],
    }


def main() -> None:
    brief_path = Path("engine/production/approved_brief.json")
    film = json.loads(brief_path.read_text(encoding="utf-8"))
    actual = compute_brief_sha256(film)
    if actual != EXPECTED_BRIEF_SHA256:
        raise SystemExit(f"brief_sha_mismatch expected={EXPECTED_BRIEF_SHA256} actual={actual}")

    moment = dict(film)
    moment["format"] = "moment"

    results = [
        _run_case("film_exact_five", film),
        _run_case("moment_validator_one_to_five", moment),
    ]
    print(
        "MISTRAL_PLANNING_SCHEMA_PROBE="
        + json.dumps(
            {"status": "pass", "cases": results},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()

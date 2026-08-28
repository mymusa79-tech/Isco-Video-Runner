from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from isco_video_agent.anti_repetition import novelty_context
from isco_video_agent.config import load_approved_brief, load_editorial_policy
from isco_video_agent.learning import learning_context
from isco_video_agent.orchestrator import planning_research_context
from isco_video_agent.providers.gemini import (
    OUTLINE_PORTABLE_MAX_PROMPT_UTF8_BYTES,
    with_channel_persona,
)
from isco_video_agent.resilient_planner import build_outline_prompt
from scripts.dynamic_planning_capacity import certify_general_planning_envelope
from scripts.planning_batch_hardening import MAX_SCRIPT_BATCH_SECTIONS
from scripts.provider_capacity_hardening import completion_token_budget, groq_capacity_estimate


@dataclass(frozen=True)
class PlanningCapacityCertification:
    status: str
    format: str
    prompt_utf8_bytes: int
    portable_limit_utf8_bytes: int
    outline_estimated_request_tokens: int
    viable_providers: list[str]
    outline_completion_reserve: int
    full_script_completion_reserve: int
    max_script_batch_sections: int
    runtime_exact_writer_gate: str


def certify_planning_capacity() -> PlanningCapacityCertification:
    """General pre-production gate; exact Writer admission runs once the real shard exists."""
    brief = load_approved_brief(required=True)
    fmt = str(brief["format"]).strip().lower()
    outline_contract = ("editorial_outline", {})
    full_script_contract = ("full_script", {})

    if fmt not in {"film", "story"}:
        return PlanningCapacityCertification(
            status="not_applicable",
            format=fmt,
            prompt_utf8_bytes=0,
            portable_limit_utf8_bytes=OUTLINE_PORTABLE_MAX_PROMPT_UTF8_BYTES,
            outline_estimated_request_tokens=0,
            viable_providers=["not_applicable"],
            outline_completion_reserve=completion_token_budget(outline_contract),
            full_script_completion_reserve=completion_token_budget(full_script_contract),
            max_script_batch_sections=MAX_SCRIPT_BATCH_SECTIONS,
            runtime_exact_writer_gate="enabled",
        )

    research = planning_research_context(brief, {})
    prompt = build_outline_prompt(
        topic=str(brief["approved_topic"]),
        fmt=fmt,
        policy_json=json.dumps(load_editorial_policy(), ensure_ascii=False),
        research_json=json.dumps(research, ensure_ascii=False),
        avoid_json=json.dumps(novelty_context(), ensure_ascii=False),
        learning_json=json.dumps(learning_context(fmt), ensure_ascii=False),
        revision_note="",
    )
    enriched = with_channel_persona(prompt)
    size = len(enriched.encode("utf-8"))
    if size > OUTLINE_PORTABLE_MAX_PROMPT_UTF8_BYTES:
        raise RuntimeError(
            "planning envelope exceeds provider-portable byte limit: "
            f"bytes={size} limit={OUTLINE_PORTABLE_MAX_PROMPT_UTF8_BYTES}"
        )
    if MAX_SCRIPT_BATCH_SECTIONS > 3:
        raise RuntimeError("long-form writer batch certification exceeds three sections")

    estimate = groq_capacity_estimate(enriched)
    viable = certify_general_planning_envelope(estimate["estimated_request_tokens"])
    return PlanningCapacityCertification(
        status="pass",
        format=fmt,
        prompt_utf8_bytes=size,
        portable_limit_utf8_bytes=OUTLINE_PORTABLE_MAX_PROMPT_UTF8_BYTES,
        outline_estimated_request_tokens=estimate["estimated_request_tokens"],
        viable_providers=viable,
        outline_completion_reserve=completion_token_budget(outline_contract),
        full_script_completion_reserve=completion_token_budget(full_script_contract),
        max_script_batch_sections=MAX_SCRIPT_BATCH_SECTIONS,
        runtime_exact_writer_gate="enabled",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = certify_planning_capacity()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(asdict(result), ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        "Planning capacity gate PASS: "
        f"status={result.status} outline_required={result.outline_estimated_request_tokens} "
        f"viable={','.join(result.viable_providers)} exact_writer_gate={result.runtime_exact_writer_gate}"
    )


if __name__ == "__main__":
    main()

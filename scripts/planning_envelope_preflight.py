from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from isco_video_agent.anti_repetition import novelty_context
from isco_video_agent.config import (
    load_approved_brief,
    load_editorial_policy,
)
from isco_video_agent.learning import learning_context
from isco_video_agent.orchestrator import planning_research_context
from isco_video_agent.providers.gemini import (
    OUTLINE_PORTABLE_MAX_PROMPT_UTF8_BYTES,
    with_channel_persona,
)
from isco_video_agent.resilient_planner import build_outline_prompt
from scripts.dynamic_planning_capacity import certify_general_planning_envelope
from scripts.immutable_planning_snapshot import bind_runtime_approved_brief_path
from scripts.planning_batch_hardening import MAX_SCRIPT_BATCH_SECTIONS
from scripts.planning_stage_contract import (
    outline_stage_spec_for_format,
    script_stage_spec,
)
from scripts.provider_capacity_hardening import (
    groq_capacity_estimate,
)


@dataclass(frozen=True)
class PlanningEnvelopeCertification:
    status: str
    format: str
    prompt_utf8_bytes: int
    portable_limit_utf8_bytes: int
    remaining_headroom_utf8_bytes: int
    approved_sources: int
    approved_boundaries: int
    outline_estimated_request_tokens: int
    groq_tpm_limit: int | None
    outline_groq_tpm_headroom: int | None
    outline_completion_reserve: int
    full_script_completion_reserve: int
    max_script_batch_sections: int
    runtime_token_admission: str


def certify_planning_envelope() -> PlanningEnvelopeCertification:
    """Certify the real general envelope and require at least one viable P0 provider.

    The exact Writer shard does not exist until the outline is produced. This gate is
    intentionally tier one: it certifies the current approved outline envelope plus the
    fixed Writer batching contract. Tier two runs on the exact first Writer shard inside
    dynamic_planning_capacity before that shard can call a provider.
    """
    # Canonical V4 materializes a read-only approved-brief snapshot during state restore.
    # A workflow step may still export the historical worktree path, so prefer the
    # snapshot explicitly whenever it exists. This keeps preflight and production on the
    # same immutable bytes even if an earlier test modified the Engine working tree.
    if str(os.environ.get("ISCO_APPROVED_BRIEF_SNAPSHOT_PATH") or "").strip():
        bind_runtime_approved_brief_path()

    brief = load_approved_brief(required=True)
    fmt = str(brief["format"]).strip().lower()

    if fmt not in {"film", "story"}:
        return PlanningEnvelopeCertification(
            status="not_applicable",
            format=fmt,
            prompt_utf8_bytes=0,
            portable_limit_utf8_bytes=OUTLINE_PORTABLE_MAX_PROMPT_UTF8_BYTES,
            remaining_headroom_utf8_bytes=OUTLINE_PORTABLE_MAX_PROMPT_UTF8_BYTES,
            approved_sources=len(brief.get("research_pack", [])),
            approved_boundaries=0,
            outline_estimated_request_tokens=0,
            groq_tpm_limit=None,
            outline_groq_tpm_headroom=None,
            outline_completion_reserve=0,
            full_script_completion_reserve=0,
            max_script_batch_sections=MAX_SCRIPT_BATCH_SECTIONS,
            runtime_token_admission="provider_set_dynamic+exact_writer",
        )

    outline_spec = outline_stage_spec_for_format(fmt)
    writer_spec = script_stage_spec(
        "full_script",
        [f"preflight-section-{index}" for index in range(1, MAX_SCRIPT_BATCH_SECTIONS + 1)],
    )
    outline_reserve = outline_spec.provider_policy.completion_tokens
    writer_reserve = writer_spec.provider_policy.completion_tokens

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
            "planning envelope exceeds provider-portable limit: "
            f"bytes={size} limit={OUTLINE_PORTABLE_MAX_PROMPT_UTF8_BYTES}"
        )
    if MAX_SCRIPT_BATCH_SECTIONS > 3:
        raise RuntimeError("long-form writer batch certification exceeds three sections")

    # The standalone workflow process has no active runtime StageSpec. Pass the exact
    # canonical Stage Contract budget explicitly so preflight cannot silently fall back
    # to provider_capacity_hardening's historical prompt-inferred 2400-token table.
    request_capacity = groq_capacity_estimate(
        enriched,
        reserved_completion_tokens=outline_reserve,
        contract_name=str(outline_spec.semantic_rules["transport_profile"]),
    )
    # This is no longer "Groq <= 8000 therefore production is safe". It asks the
    # provider set whether one path is currently not known incapable of the P0 envelope.
    certify_general_planning_envelope(request_capacity["estimated_request_tokens"])

    groq_limit = request_capacity.get("provider_tpm_limit")
    headroom = (
        int(groq_limit) - int(request_capacity["estimated_request_tokens"])
        if isinstance(groq_limit, int)
        else None
    )

    return PlanningEnvelopeCertification(
        status="pass",
        format=fmt,
        prompt_utf8_bytes=size,
        portable_limit_utf8_bytes=OUTLINE_PORTABLE_MAX_PROMPT_UTF8_BYTES,
        remaining_headroom_utf8_bytes=OUTLINE_PORTABLE_MAX_PROMPT_UTF8_BYTES - size,
        approved_sources=len(research.get("approved_research_pack", [])),
        approved_boundaries=len(research.get("content_boundaries", [])),
        outline_estimated_request_tokens=request_capacity["estimated_request_tokens"],
        groq_tpm_limit=groq_limit,
        outline_groq_tpm_headroom=headroom,
        outline_completion_reserve=outline_reserve,
        full_script_completion_reserve=writer_reserve,
        max_script_batch_sections=MAX_SCRIPT_BATCH_SECTIONS,
        runtime_token_admission="provider_set_dynamic+exact_writer",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = certify_planning_envelope()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(asdict(result), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        "Planning envelope certified: "
        f"status={result.status} bytes={result.prompt_utf8_bytes} "
        f"limit={result.portable_limit_utf8_bytes} "
        f"byte_headroom={result.remaining_headroom_utf8_bytes} "
        f"required_tokens={result.outline_estimated_request_tokens} "
        f"groq_observed_or_initial_limit={result.groq_tpm_limit} "
        f"runtime_admission={result.runtime_token_admission} "
        f"script_batch_max={result.max_script_batch_sections}"
    )


if __name__ == "__main__":
    main()

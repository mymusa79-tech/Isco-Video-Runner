from __future__ import annotations

import argparse
import json
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
from scripts.planning_batch_hardening import MAX_SCRIPT_BATCH_SECTIONS
from scripts.provider_capacity_hardening import (
    GROQ_FREE_TPM_LIMIT,
    completion_token_budget,
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
    groq_tpm_limit: int
    outline_groq_tpm_headroom: int
    outline_completion_reserve: int
    full_script_completion_reserve: int
    max_script_batch_sections: int
    runtime_token_admission: str


def certify_planning_envelope() -> PlanningEnvelopeCertification:
    """Build the real approved outline envelope locally without an inference call.

    The exact later writer prompt depends on the provider-produced outline, so it
    cannot honestly be reconstructed before production. Preflight therefore certifies
    the exact outline plus the immutable writer batching/token-budget policy; every
    real later request is re-checked by the runtime token-capacity admission guard.
    """
    brief = load_approved_brief(required=True)
    fmt = str(brief["format"]).strip().lower()
    outline_contract = ("editorial_outline", {})
    full_script_contract = ("full_script", {})

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
            groq_tpm_limit=GROQ_FREE_TPM_LIMIT,
            outline_groq_tpm_headroom=GROQ_FREE_TPM_LIMIT,
            outline_completion_reserve=completion_token_budget(outline_contract),
            full_script_completion_reserve=completion_token_budget(full_script_contract),
            max_script_batch_sections=MAX_SCRIPT_BATCH_SECTIONS,
            runtime_token_admission="enabled",
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
        # with_channel_persona() normally raises first; retain an explicit local
        # assertion so this preflight remains fail-closed if its implementation moves.
        raise RuntimeError(
            "planning envelope exceeds provider-portable limit: "
            f"bytes={size} limit={OUTLINE_PORTABLE_MAX_PROMPT_UTF8_BYTES}"
        )

    capacity = groq_capacity_estimate(enriched)
    if capacity["estimated_request_tokens"] > GROQ_FREE_TPM_LIMIT:
        raise RuntimeError(
            "outline envelope exceeds Groq free TPM capacity estimate: "
            f"estimated={capacity['estimated_request_tokens']} limit={GROQ_FREE_TPM_LIMIT}"
        )
    if MAX_SCRIPT_BATCH_SECTIONS > 3:
        raise RuntimeError("long-form writer batch certification exceeds three sections")

    return PlanningEnvelopeCertification(
        status="pass",
        format=fmt,
        prompt_utf8_bytes=size,
        portable_limit_utf8_bytes=OUTLINE_PORTABLE_MAX_PROMPT_UTF8_BYTES,
        remaining_headroom_utf8_bytes=OUTLINE_PORTABLE_MAX_PROMPT_UTF8_BYTES - size,
        approved_sources=len(research.get("approved_research_pack", [])),
        approved_boundaries=len(research.get("content_boundaries", [])),
        outline_estimated_request_tokens=capacity["estimated_request_tokens"],
        groq_tpm_limit=GROQ_FREE_TPM_LIMIT,
        outline_groq_tpm_headroom=GROQ_FREE_TPM_LIMIT - capacity["estimated_request_tokens"],
        outline_completion_reserve=completion_token_budget(outline_contract),
        full_script_completion_reserve=completion_token_budget(full_script_contract),
        max_script_batch_sections=MAX_SCRIPT_BATCH_SECTIONS,
        runtime_token_admission="enabled",
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
        f"groq_estimated_tokens={result.outline_estimated_request_tokens} "
        f"groq_tpm_headroom={result.outline_groq_tpm_headroom} "
        f"script_batch_max={result.max_script_batch_sections}"
    )


if __name__ == "__main__":
    main()

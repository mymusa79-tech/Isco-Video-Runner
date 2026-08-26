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


@dataclass(frozen=True)
class PlanningEnvelopeCertification:
    status: str
    format: str
    prompt_utf8_bytes: int
    portable_limit_utf8_bytes: int
    remaining_headroom_utf8_bytes: int
    approved_sources: int
    approved_boundaries: int


def certify_planning_envelope() -> PlanningEnvelopeCertification:
    """Build the real approved outline envelope locally without an inference call."""
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
    return PlanningEnvelopeCertification(
        status="pass",
        format=fmt,
        prompt_utf8_bytes=size,
        portable_limit_utf8_bytes=OUTLINE_PORTABLE_MAX_PROMPT_UTF8_BYTES,
        remaining_headroom_utf8_bytes=OUTLINE_PORTABLE_MAX_PROMPT_UTF8_BYTES - size,
        approved_sources=len(research.get("approved_research_pack", [])),
        approved_boundaries=len(research.get("content_boundaries", [])),
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
        f"headroom={result.remaining_headroom_utf8_bytes}"
    )


if __name__ == "__main__":
    main()

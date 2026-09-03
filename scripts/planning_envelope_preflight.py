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
from scripts.dynamic_planning_capacity import (
    viable_planning_providers,
    viable_provider_families,
)
from scripts.immutable_planning_snapshot import bind_runtime_approved_brief_path
from scripts.native_short_planner_router import (
    merge_short_template_revision,
    select_native_short_template,
)
from scripts.p0_runtime_master_contract import activate_p0_runtime_master
from scripts.planning_batch_hardening import MAX_SCRIPT_BATCH_SECTIONS
from scripts.planning_capacity_profile import install_planning_capacity_profile
from scripts.producer_quality_contract import merge_producer_revision_note
from scripts.provider_capacity_margin_audit import audit_media_capacity_margin

# Standalone preflight runs in a different Python process from production runtime.
# Apply the exact same format-native caps before importing the headroom module's public
# constants/builders so certification and live execution cannot drift.
install_planning_capacity_profile()

from scripts.planning_capacity_headroom import (  # noqa: E402
    SHORT_EFFECTIVE_PROMPT_MAX_UTF8_BYTES,
    build_short_initial_prompt,
    certify_short_prompt_envelope,
    groq_operational_headroom_tokens,
    worst_case_short_review_capacity,
)
from scripts.planning_stage_contract import (
    outline_stage_spec_for_format,
    script_stage_spec,
)
from scripts.provider_capacity_hardening import (
    groq_capacity_estimate,
    groq_effective_tpm_limit,
)


P0_OUTLINE_MIN_PROVIDER_FAMILIES = 2
P0_SHORT_MIN_PROVIDER_FAMILIES = 2


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
    viable_provider_families: tuple[str, ...]
    required_provider_families: int
    runtime_token_admission: str


def _headroom_filtered_viable(required_tokens: int) -> list[str]:
    """Apply the runtime Groq operational margin in the standalone preflight process."""
    viable = viable_planning_providers(required_tokens)
    filtered: list[str] = []
    for provider in viable:
        if not provider.startswith("groq:"):
            filtered.append(provider)
            continue
        model = provider.split(":", 1)[1]
        limit = groq_effective_tpm_limit(model)
        operational = groq_operational_headroom_tokens(limit)
        if isinstance(limit, int) and int(required_tokens) + operational > limit:
            continue
        filtered.append(provider)
    return filtered


def _require_provider_redundancy(
    required_tokens: int,
    *,
    phase: str,
    required_families: int,
) -> tuple[list[str], tuple[str, ...]]:
    viable = _headroom_filtered_viable(required_tokens)
    families = tuple(viable_provider_families(viable))
    if len(families) < required_families:
        raise RuntimeError(
            "PLANNING_CAPACITY_REDUNDANCY_REQUIRED "
            f"phase={phase} required_tokens={int(required_tokens)} "
            f"viable_families={','.join(families) if families else 'none'} "
            f"required_families={required_families}"
        )
    return viable, families


def compose_short_production_revision(
    topic: object,
    research_context: dict | None,
) -> tuple[dict[str, object], str]:
    """Mirror the live Producer -> native Short wrapper composition order."""
    selection = select_native_short_template(topic)
    producer_revision = merge_producer_revision_note("", research_context)
    revision = merge_short_template_revision(
        str(selection["template"]),
        producer_revision,
    )
    return selection, revision


def _certify_short_envelope(brief: dict, research: dict) -> PlanningEnvelopeCertification:
    topic = str(brief["approved_topic"])
    _, revision = compose_short_production_revision(topic, research)
    prompt = build_short_initial_prompt(
        topic=topic,
        research_context=research,
        avoid_context=novelty_context(),
        revision_note=revision,
    )
    initial_capacity = certify_short_prompt_envelope(
        prompt,
        phase="preflight_initial",
    )
    initial_size = int(initial_capacity["effective_prompt_utf8_bytes"])
    review_capacity = worst_case_short_review_capacity(
        topic,
        revision_note=revision,
    )
    required_tokens = max(
        int(initial_capacity["estimated_request_tokens"]),
        int(review_capacity["estimated_request_tokens"]),
    )
    max_size = max(
        initial_size,
        int(review_capacity["effective_prompt_utf8_bytes"]),
    )

    _, families = _require_provider_redundancy(
        required_tokens,
        phase="preproduction_short_envelope",
        required_families=P0_SHORT_MIN_PROVIDER_FAMILIES,
    )

    groq_limit = initial_capacity.get("provider_tpm_limit")
    raw_headroom = (
        int(groq_limit) - required_tokens
        if isinstance(groq_limit, int)
        else None
    )
    return PlanningEnvelopeCertification(
        status="pass",
        format="moment",
        prompt_utf8_bytes=max_size,
        portable_limit_utf8_bytes=SHORT_EFFECTIVE_PROMPT_MAX_UTF8_BYTES,
        remaining_headroom_utf8_bytes=SHORT_EFFECTIVE_PROMPT_MAX_UTF8_BYTES - max_size,
        approved_sources=len(research.get("approved_research_pack", [])),
        approved_boundaries=len(research.get("content_boundaries", [])),
        outline_estimated_request_tokens=required_tokens,
        groq_tpm_limit=groq_limit,
        outline_groq_tpm_headroom=raw_headroom,
        outline_completion_reserve=max(
            int(initial_capacity["reserved_completion_tokens"]),
            int(review_capacity["reserved_completion_tokens"]),
        ),
        full_script_completion_reserve=0,
        max_script_batch_sections=0,
        viable_provider_families=families,
        required_provider_families=P0_SHORT_MIN_PROVIDER_FAMILIES,
        runtime_token_admission=(
            "p0_two_provider_families+groq_operational_headroom+"
            "format_native_short_envelope+single_reset_recovery"
        ),
    )


def certify_planning_envelope() -> PlanningEnvelopeCertification:
    """Certify provider-portable capacity before a real production inference call.

    Long-form certifies the exact current outline plus Writer batching contract. Moment
    certifies its own format-native Draft envelope plus a worst-case bounded Review
    envelope. Both paths apply the same Groq operational headroom used by live runtime.
    The immediately preceding provider-preflight result is also checked for observable
    media request-count headroom, so `remaining > 0` is never treated as enough for a
    multi-search production topology.
    """
    if str(os.environ.get("ISCO_APPROVED_BRIEF_SNAPSHOT_PATH") or "").strip():
        bind_runtime_approved_brief_path()

    audit_media_capacity_margin()

    brief = load_approved_brief(required=True)
    fmt = str(brief["format"]).strip().lower()
    research = planning_research_context(brief, {})

    if fmt == "moment":
        return _certify_short_envelope(brief, research)

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
            viable_provider_families=(),
            required_provider_families=0,
            runtime_token_admission="provider_set_dynamic+exact_writer",
        )

    outline_spec = outline_stage_spec_for_format(fmt)
    writer_spec = script_stage_spec(
        "full_script",
        [f"preflight-section-{index}" for index in range(1, MAX_SCRIPT_BATCH_SECTIONS + 1)],
    )
    outline_reserve = outline_spec.provider_policy.completion_tokens
    writer_reserve = writer_spec.provider_policy.completion_tokens

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

    request_capacity = groq_capacity_estimate(
        enriched,
        reserved_completion_tokens=outline_reserve,
        contract_name=str(outline_spec.semantic_rules["transport_profile"]),
    )
    _, families = _require_provider_redundancy(
        int(request_capacity["estimated_request_tokens"]),
        phase="preproduction_general_envelope",
        required_families=P0_OUTLINE_MIN_PROVIDER_FAMILIES,
    )

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
        viable_provider_families=families,
        required_provider_families=P0_OUTLINE_MIN_PROVIDER_FAMILIES,
        runtime_token_admission=(
            "p0_two_provider_families+groq_operational_headroom+"
            "provider_set_dynamic+exact_writer+observable_media_margin"
        ),
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
    if result.status == "pass":
        # P0 Runtime Master V2: the planning envelope is the last canonical readiness
        # gate in Production V4. Promote live runtime only after its evidence is durable,
        # so Environment, State, Provider Readiness and Planning all agree on one phase
        # boundary. Non-canonical invocations are a no-op inside the master contract.
        activate_p0_runtime_master()
    print(
        "Planning envelope certified: "
        f"status={result.status} bytes={result.prompt_utf8_bytes} "
        f"limit={result.portable_limit_utf8_bytes} "
        f"byte_headroom={result.remaining_headroom_utf8_bytes} "
        f"required_tokens={result.outline_estimated_request_tokens} "
        f"groq_observed_or_initial_limit={result.groq_tpm_limit} "
        f"viable_families={','.join(result.viable_provider_families) if result.viable_provider_families else 'n/a'} "
        f"required_families={result.required_provider_families} "
        f"runtime_admission={result.runtime_token_admission} "
        f"script_batch_max={result.max_script_batch_sections}"
    )


if __name__ == "__main__":
    main()
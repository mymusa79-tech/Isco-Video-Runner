from __future__ import annotations

import re

from . import planning_batch_hardening as batch
from . import provider_capacity_hardening as capacity
from . import run120_dossier_repair_hardening as dossier
from . import task_level_planner_router as router


# Run #122 reached the Engine RepairDossier twice and proved the Run #120/#121
# checkpoint + adaptive-shard machinery itself.  It still exhausted the P1 budget
# frontier because capacity admission happened before task_router appended the channel
# persona, so ten requests that were already too large for Groq crossed the provider
# boundary and then spent real OpenRouter attempts ending in finish_reason=length.
#
# Keep the Engine's 42-attempt hard cap and its four-attempt P0 reserve unchanged.
# Instead, make admission use the exact routed prompt and reserve completion headroom
# proportional to the exact number of sections in a bounded full_script shard.  The
# values below are intentionally conservative relative to successful production
# telemetry while avoiding a flat 2400-token reserve for every one-section repair.
_GROQ_FULL_SCRIPT_COMPLETION_BY_SECTIONS = {
    1: 1500,
    2: 2100,
    3: 2200,
}

_BASE_COMPLETION_TOKEN_BUDGET = capacity.completion_token_budget
_ID_BOUNDARY = r"[\w-]"


def _full_script_section_count(contract: tuple[str, dict] | None) -> int | None:
    if not contract or contract[0] != "full_script":
        return None
    try:
        sections = contract[1]["properties"]["sections"]
        minimum = int(sections["minItems"])
        maximum = int(sections["maxItems"])
    except (KeyError, TypeError, ValueError):
        return None
    if minimum != maximum or minimum <= 0:
        return None
    return minimum


def _groq_completion_token_budget(contract: tuple[str, dict] | None) -> int:
    count = _full_script_section_count(contract)
    if count in _GROQ_FULL_SCRIPT_COMPLETION_BY_SECTIONS:
        return _GROQ_FULL_SCRIPT_COMPLETION_BY_SECTIONS[count]
    return _BASE_COMPLETION_TOKEN_BUDGET(contract)


def _direct_groq_capacity_estimate(prompt: str) -> dict:
    """Estimate the exact prompt already visible at the Groq provider boundary."""
    contract = router._structured_schema_for_prompt(prompt)
    prompt_tokens = capacity.estimate_prompt_tokens(prompt)
    reserved_completion = _groq_completion_token_budget(contract)
    estimated_total = prompt_tokens + reserved_completion + capacity.GROQ_TOKEN_SAFETY_RESERVE
    return {
        "estimated_prompt_tokens": prompt_tokens,
        "reserved_completion_tokens": reserved_completion,
        "token_safety_reserve": capacity.GROQ_TOKEN_SAFETY_RESERVE,
        "estimated_request_tokens": estimated_total,
        "provider_tpm_limit": capacity.GROQ_FREE_TPM_LIMIT,
        "contract": contract[0] if contract else "json_object",
    }


def _routed_prompt(prompt: str) -> str:
    """Mirror task_router's enrichment before any provider is selected."""
    enriched = router._enrich_dialogue_prompt(prompt)
    return router.with_channel_persona(enriched)


def _routed_groq_capacity_estimate(prompt: str) -> dict:
    return _direct_groq_capacity_estimate(_routed_prompt(prompt))


def _capacity_admitted_after_enrichment(prompt: str) -> tuple[bool, dict]:
    estimate = _routed_groq_capacity_estimate(prompt)
    return estimate["estimated_request_tokens"] <= capacity.GROQ_FREE_TPM_LIMIT, estimate


def _ids_named_in_line(line: str, current_ids: list[str]) -> list[str]:
    found: list[str] = []
    for section_id in current_ids:
        pattern = rf"(?<!{_ID_BOUNDARY}){re.escape(section_id)}(?!{_ID_BOUNDARY})"
        if re.search(pattern, line, flags=re.IGNORECASE):
            found.append(section_id)
    return found


def _conservative_issue_target_ids(
    issue_notes: str,
    current_ids: list[str],
    *,
    explicit_resolver,
) -> list[str] | None:
    """Return a local repair frontier only when every blocking line proves its scope.

    The Engine's explicit TARGET_SECTION_IDS remains authoritative.  Without that
    marker, structure and semantic-repetition lines can carry exact section ids.
    Runner's spoken-hook novelty guard makes hook_too_similar_to_recent a deterministic
    first-section defect.  Any aggregate or unscoped issue keeps the pre-existing
    global repair path; we never guess a section merely to save calls.
    """
    explicit = explicit_resolver(issue_notes, current_ids)
    if explicit is not None:
        return explicit

    lines = [line.strip() for line in str(issue_notes or "").splitlines() if line.strip().startswith("- [")]
    if not lines or not current_ids:
        return None

    targets: set[str] = set()
    for line in lines:
        lowered = line.casefold()
        if "__aggregate__" in lowered:
            return None

        line_ids = _ids_named_in_line(line, current_ids)
        if "[novelty]" in lowered and "hook_too_similar_to_recent" in lowered:
            line_ids = [*line_ids, current_ids[0]]

        if not line_ids:
            return None
        targets.update(line_ids)

    ordered = [section_id for section_id in current_ids if section_id in targets]
    return ordered or None


def install_run122_budget_aware_repair() -> None:
    """Install the Run #122 closure without changing budget or quality policy.

    This installer deliberately runs after run120_schema_policy_bridge assigns the
    existing schema owner.  We wrap transport admission only; schema repair, provider
    retries, RepairDossier max_attempts=2, reaudits, BudgetLedger enforcement and final
    Engine gates remain unchanged.
    """
    if getattr(dossier, "_ISCO_RUN122_BUDGET_AWARE_REPAIR", False):
        return

    original_groq_call = router._groq_call
    original_request_metadata = router._request_metadata
    original_dossier_call = dossier._one_schema_bounded_call
    original_target_ids = dossier._target_ids

    def dynamic_groq_call(prompt: str):
        # provider_capacity_hardening's Groq function already owns HTTP, pacing and
        # BudgetLedger accounting.  It reads these two helpers synchronously, so swap
        # only for the duration of this single sequential planning provider call and
        # restore them even on failure.  OpenRouter keeps its existing output budget.
        previous_estimate = capacity.groq_capacity_estimate
        previous_budget = capacity.completion_token_budget
        capacity.groq_capacity_estimate = _direct_groq_capacity_estimate
        capacity.completion_token_budget = _groq_completion_token_budget
        try:
            return original_groq_call(prompt)
        finally:
            capacity.groq_capacity_estimate = previous_estimate
            capacity.completion_token_budget = previous_budget

    def dynamic_request_metadata(prompt: str) -> dict:
        metadata = original_request_metadata(prompt)
        metadata.update(_direct_groq_capacity_estimate(prompt))
        return metadata

    def capacity_guarded_dossier_call(
        api_key: str,
        prompt: str,
        model: str,
        expected_ids: list[str],
    ) -> dict[str, dict]:
        estimate = _routed_groq_capacity_estimate(prompt)
        if estimate["estimated_request_tokens"] > capacity.GROQ_FREE_TPM_LIMIT:
            raise dossier._DossierTransportPressure(
                "DOSSIER_PROVIDER_PORTABILITY_PREFLIGHT "
                f"sections={','.join(expected_ids)} "
                f"estimated_total={estimate['estimated_request_tokens']} "
                f"limit={capacity.GROQ_FREE_TPM_LIMIT}"
            )
        return original_dossier_call(api_key, prompt, model, expected_ids)

    def conservative_target_ids(issue_notes: str, current_ids: list[str]) -> list[str] | None:
        return _conservative_issue_target_ids(
            issue_notes,
            current_ids,
            explicit_resolver=original_target_ids,
        )

    # Base Writer/Doctor admission now sees the same persona-enriched prompt the
    # provider router sees.  This converts capacity failures into zero-call splits.
    batch._capacity_admitted = _capacity_admitted_after_enrichment

    # Groq keeps the same HTTP/retry owner and 8K TPM hard boundary; only the reserved
    # completion envelope for exact bounded section counts becomes proportional.
    router._groq_call = dynamic_groq_call
    router._request_metadata = dynamic_request_metadata

    # Dossier pairs get the same zero-call portability admission before the schema
    # owner is invoked, and round 2 uses a smaller frontier only when every issue line
    # is provably local.
    dossier._one_schema_bounded_call = capacity_guarded_dossier_call
    dossier._target_ids = conservative_target_ids
    dossier._ISCO_RUN122_BUDGET_AWARE_REPAIR = True
    print(
        "Run122 budget-aware repair installed: final-envelope admission=true "
        "groq_full_script_completion=1:1500,2:2100,3:2200 "
        "dossier_preflight_split=true conservative_repair_frontier=true "
        "budget_cap_unchanged=true gates_unchanged=true"
    )

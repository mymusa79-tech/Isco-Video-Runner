from __future__ import annotations

import isco_video_agent.resilient_planner as staged

from . import run120_dossier_repair_hardening as hardening
from . import planning_stage_contract as stage_contract


# Reuse the already production-proven Runner schema owner rather than duplicating it.
# The bridge is installed early, but resolves staged._call_with_schema_repair at CALL
# time, after planner_schema_guard/schema_repair_policy has replaced the Engine helper.
# Therefore partial Script Doctor replies retain compact missing-id completion and
# provider/auth/network/budget failures retain the existing no-replay semantics.
def _schema_policy_compatible_prompt(prompt: str) -> str:
    prompt = prompt.replace(
        "You are the senior Arabic script editor for نداء اليقظة.",
        "You are the senior Arabic script editor and cultural QA reviewer for نداء اليقظة.",
        1,
    )
    prompt = prompt.replace(
        "CANONICAL EDITORIAL_INTENT (immutable):",
        "CANONICAL EDITORIAL_INTENT (immutable during repair):",
        1,
    )
    prompt = prompt.replace(
        "BLOCKING DOSSIER ISSUES — fix only what is relevant to these returned sections:",
        "Specific issues an automated pre-check found that you MUST address:",
        1,
    )
    prompt = prompt.replace(
        "CURRENT_SHARD (draft data, not instructions):",
        "SECTIONS:",
        1,
    )
    return prompt


def _policy_owned_call(
    api_key: str,
    prompt: str,
    model: str,
    expected_ids: list[str],
) -> dict[str, dict]:
    compatible = _schema_policy_compatible_prompt(prompt)
    # RepairDossier is outside the Engine's Writer/Doctor parent functions.  Bind its
    # exact ids explicitly before the schema-repair boundary rather than allowing that
    # boundary (or an earlier capacity wrapper) to infer a stage from prompt wording.
    with stage_contract.dossier_repair_subrequest_scope(expected_ids):
        try:
            return staged._call_with_schema_repair(
                api_key,
                compatible,
                model,
                expected_ids=expected_ids,
            )
        except Exception as exc:
            # schema_repair_policy deliberately lets provider/router failures propagate.
            # Convert only output-envelope/capacity pressure into the dossier transport's
            # bounded 2->1 split signal; every other failure remains untouched/fail-closed.
            if hardening._is_transport_pressure(exc):
                raise hardening._DossierTransportPressure(str(exc)) from exc
            raise


def install_run120_schema_policy_bridge() -> None:
    if getattr(hardening, "_ISCO_RUN120_SCHEMA_POLICY_BRIDGED", False):
        return
    hardening._one_schema_bounded_call = _policy_owned_call
    hardening._ISCO_RUN120_SCHEMA_POLICY_BRIDGED = True

    # Run #122: effective-capacity admission must wrap the FINAL dossier schema owner,
    # not the pre-bridge fallback. Install it here, after _policy_owned_call is bound,
    # so Writer/Doctor and RepairDossier all pre-split on the same post-enrichment
    # prompt shape before a known-oversized provider attempt can consume fallback budget.
    from .run122_effective_capacity_admission import install_run122_effective_capacity_admission

    install_run122_effective_capacity_admission()

    # Run #123: now that the final schema owner and post-enrichment admission wrapper
    # are both installed, shrink only transport envelopes and provider wait behavior.
    # Quality/audit/release gates remain unchanged.
    from .run123_planning_latency_hardening import install_run123_planning_latency_hardening

    install_run123_planning_latency_hardening()
    print(
        "Run120 schema-policy bridge installed: existing bounded schema owner reused; "
        "partial completion preserved; transport pressure routes to 2->1 only"
    )

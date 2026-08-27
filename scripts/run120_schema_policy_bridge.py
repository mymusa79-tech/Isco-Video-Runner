from __future__ import annotations

import isco_video_agent.resilient_planner as staged

from . import provider_capacity_hardening as capacity
from . import run120_dossier_repair_hardening as hardening


# Reuse the already production-proven Runner schema owner rather than duplicating it.
# The bridge is installed early, but resolves staged._call_with_schema_repair at CALL
# time, after planner_schema_guard/schema_repair_policy has replaced the Engine helper.
# Therefore partial Script Doctor replies retain compact missing-id completion and
# provider/auth/network/budget failures retain the existing no-replay semantics.
#
# Run #122 proved that RepairDossier also needs the same *pre-provider* admission used
# by base Writer/Doctor: otherwise a known-oversized pair consumes a Groq capacity
# rejection plus an OpenRouter length failure before the existing 2->1 transport split.
# Admission below reuses provider_capacity_hardening.groq_capacity_estimate verbatim;
# it does not own retries, change provider order, raise budgets, or weaken any Engine
# dossier/reaudit gate.

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


def _admit_dossier_shard(prompt: str, expected_ids: list[str]) -> dict:
    """Fail with the existing transport-split signal before any provider attempt.

    The Groq Free 8K TPM envelope is the portability floor used throughout planning.
    A multi-section shard above that envelope is not sent to the router at all; the
    existing Run120 repair transport catches _DossierTransportPressure and splits only
    that shard to singles. A single-section shard above the envelope receives the same
    signal and therefore fails closed rather than bypassing capacity safety.
    """
    estimate = capacity.groq_capacity_estimate(prompt)
    if estimate["estimated_request_tokens"] <= capacity.GROQ_FREE_TPM_LIMIT:
        return estimate

    ids = [str(section_id) for section_id in expected_ids]
    print(
        "Dossier capacity split before provider call: "
        f"sections={','.join(ids)} "
        f"estimated_total={estimate['estimated_request_tokens']} "
        f"limit={capacity.GROQ_FREE_TPM_LIMIT}"
    )
    raise hardening._DossierTransportPressure(
        "DOSSIER_TPM_CAPACITY_PREFLIGHT "
        f"sections={','.join(ids)} "
        f"contract={estimate['contract']} "
        f"estimated_prompt_tokens={estimate['estimated_prompt_tokens']} "
        f"reserved_completion_tokens={estimate['reserved_completion_tokens']} "
        f"safety_tokens={estimate['token_safety_reserve']} "
        f"estimated_total={estimate['estimated_request_tokens']} "
        f"limit={capacity.GROQ_FREE_TPM_LIMIT}"
    )


def _policy_owned_call(
    api_key: str,
    prompt: str,
    model: str,
    expected_ids: list[str],
) -> dict[str, dict]:
    compatible = _schema_policy_compatible_prompt(prompt)
    _admit_dossier_shard(compatible, expected_ids)
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
    print(
        "Run120 schema-policy bridge installed: existing bounded schema owner reused; "
        "partial completion preserved; pre-provider capacity admission=true; "
        "transport pressure routes to 2->1 only"
    )

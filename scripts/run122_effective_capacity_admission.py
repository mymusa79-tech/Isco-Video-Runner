from __future__ import annotations

from scripts import planning_batch_hardening as planning
from scripts import provider_capacity_hardening as capacity
from scripts import run120_dossier_repair_hardening as dossier
from scripts import task_level_planner_router as router


def _effective_routed_prompt(prompt: str) -> str:
    """Return the exact text shape the planner router measures/sends to providers.

    Run #122 proved that pre-admission on the raw Writer/Doctor/Dossier prompt can be
    optimistic: task_router() adds dialogue enrichment and the channel persona before
    provider dispatch. Admission must therefore measure the same transformed prompt the
    router will use, without issuing a provider call.
    """
    return router.with_channel_persona(router._enrich_dialogue_prompt(prompt))


def _effective_capacity_estimate(prompt: str) -> dict:
    return capacity.groq_capacity_estimate(_effective_routed_prompt(prompt))


def _provider_set_viable(required_tokens: int) -> list[str]:
    # Function-local import avoids a module cycle: dynamic_planning_capacity itself
    # imports planning_batch_hardening. By execution time both modules are initialized.
    from scripts.dynamic_planning_capacity import viable_planning_providers

    return viable_planning_providers(int(required_tokens))


def _effective_capacity_admitted(prompt: str) -> tuple[bool, dict]:
    estimate = _effective_capacity_estimate(prompt)
    viable = _provider_set_viable(estimate["estimated_request_tokens"])
    estimate["viable_providers"] = viable
    return bool(viable), estimate


def install_run122_effective_capacity_admission() -> None:
    """Make adaptive planning split before a known-unserviceable provider-set request.

    The historical 8000 Groq number is no longer a global planning authority. Before
    the first Groq contact it remains only that provider/model's bootstrap fallback;
    Writer/Doctor/Dossier admission asks whether at least one currently available
    planning path can carry the effective routed prompt.
    """
    if getattr(planning, "_ISCO_RUN122_EFFECTIVE_CAPACITY_ADMISSION", False):
        return

    planning._capacity_admitted = _effective_capacity_admitted
    original_dossier_call = dossier._one_schema_bounded_call

    def effective_dossier_call(
        api_key: str,
        prompt: str,
        model: str,
        expected_ids: list[str],
    ) -> dict[str, dict]:
        estimate = _effective_capacity_estimate(prompt)
        viable = _provider_set_viable(estimate["estimated_request_tokens"])
        if not viable:
            raise dossier._DossierTransportPressure(
                "NO_VIABLE_PLANNING_CAPACITY effective_routed_prompt=true "
                "phase=dossier_transport "
                f"contract={estimate['contract']} "
                f"estimated_prompt_tokens={estimate['estimated_prompt_tokens']} "
                f"reserved_completion_tokens={estimate['reserved_completion_tokens']} "
                f"safety_tokens={estimate['token_safety_reserve']} "
                f"estimated_total={estimate['estimated_request_tokens']}"
            )
        return original_dossier_call(api_key, prompt, model, expected_ids)

    dossier._one_schema_bounded_call = effective_dossier_call
    planning._ISCO_RUN122_EFFECTIVE_CAPACITY_ADMISSION = True
    print(
        "Run122 effective capacity admission installed: "
        "writer_doctor=post-enrichment dossier=post-enrichment provider_set=true "
        "split_before_provider=true budget_cap=unchanged"
    )

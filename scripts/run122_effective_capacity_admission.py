from __future__ import annotations

from scripts import planning_batch_hardening as planning
from scripts import provider_capacity_hardening as capacity
from scripts import run120_dossier_repair_hardening as dossier
from scripts import task_level_planner_router as router


def _effective_routed_prompt(prompt: str) -> str:
    """Return the exact text shape the planner router measures/sends to providers.

    Run #122 proved that pre-admission on the raw Writer/Doctor/Dossier prompt can be
    optimistic: task_router() adds dialogue enrichment and the channel persona before
    provider dispatch.  A shard that looked <=8K TPM before those additions could then
    fail Groq's no-HTTP capacity preflight, spend an unnecessary OpenRouter attempt,
    and only afterwards split.  Admission must therefore measure the same transformed
    prompt the router will use, without issuing a provider call.
    """
    return router.with_channel_persona(router._enrich_dialogue_prompt(prompt))


def _effective_capacity_estimate(prompt: str) -> dict:
    return capacity.groq_capacity_estimate(_effective_routed_prompt(prompt))


def _effective_capacity_admitted(prompt: str) -> tuple[bool, dict]:
    estimate = _effective_capacity_estimate(prompt)
    return estimate["estimated_request_tokens"] <= capacity.GROQ_FREE_TPM_LIMIT, estimate


def install_run122_effective_capacity_admission() -> None:
    """Make adaptive planning split before a known-oversized provider attempt.

    This patch changes transport admission only.  It does not raise the 42-attempt Film
    hard cap, alter P0/P1 reserves, add retries, enable paid models, or weaken schema,
    editorial, factuality, tone, section-length, TTS, Vision, Director or Gold gates.

    Writer/Doctor already call planning._capacity_admitted before their router call, so
    replace that estimator with the effective routed-prompt estimator.  RepairDossier
    uses a separate bounded transport; wrap its final schema owner after the Run #120
    schema bridge is installed and convert the same local no-HTTP capacity finding into
    its existing _DossierTransportPressure signal.  Existing 2->1 split/checkpoint
    semantics then apply and successful siblings are never replayed.
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
        if estimate["estimated_request_tokens"] > capacity.GROQ_FREE_TPM_LIMIT:
            raise dossier._DossierTransportPressure(
                "GROQ_TPM_CAPACITY_PREFLIGHT effective_routed_prompt=true "
                f"contract={estimate['contract']} "
                f"estimated_prompt_tokens={estimate['estimated_prompt_tokens']} "
                f"reserved_completion_tokens={estimate['reserved_completion_tokens']} "
                f"safety_tokens={estimate['token_safety_reserve']} "
                f"estimated_total={estimate['estimated_request_tokens']} "
                f"limit={capacity.GROQ_FREE_TPM_LIMIT}"
            )
        return original_dossier_call(api_key, prompt, model, expected_ids)

    dossier._one_schema_bounded_call = effective_dossier_call
    planning._ISCO_RUN122_EFFECTIVE_CAPACITY_ADMISSION = True
    print(
        "Run122 effective capacity admission installed: "
        "writer_doctor=post-enrichment dossier=post-enrichment split_before_provider=true budget_cap=unchanged"
    )

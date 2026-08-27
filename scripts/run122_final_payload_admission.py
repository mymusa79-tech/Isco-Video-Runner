from __future__ import annotations

from scripts import planning_batch_hardening as batching
from scripts import provider_capacity_hardening as capacity
from scripts import run120_dossier_repair_hardening as dossier
from scripts import task_level_planner_router as router


# Run #122 exposed a middleware-order gap in Run #121's proactive admission. Writer,
# Doctor and RepairDossier build a raw semantic prompt, but task_router then adds the
# dialogue contract (when relevant) and channel persona before the provider-capacity
# guard sees the request. A raw shard could therefore look <= 8K while the exact routed
# payload was > 8K. Groq correctly rejected it locally, but the router then spent a real
# OpenRouter attempt that truncated before the existing adaptive split ran.
#
# Admission must measure the exact final prompt shape that the router will send. This
# module changes only that admission boundary. Provider order, retries, BudgetLedger,
# schema recovery, RepairDossier rounds and all Engine quality gates remain unchanged.

_INSTALLED = False


def _prepare_final_routed_prompt(prompt: str) -> str:
    """Mirror task_router's pure prompt middleware without issuing a provider call."""
    prepared = router._enrich_dialogue_prompt(prompt)
    return router.with_channel_persona(prepared)


def routed_groq_capacity_estimate(prompt: str) -> dict:
    """Estimate the exact post-middleware prompt that reaches provider routing."""
    return capacity.groq_capacity_estimate(_prepare_final_routed_prompt(prompt))


def _routed_capacity_admitted(prompt: str) -> tuple[bool, dict]:
    estimate = routed_groq_capacity_estimate(prompt)
    return estimate["estimated_request_tokens"] <= capacity.GROQ_FREE_TPM_LIMIT, estimate


def _dossier_capacity_guard(delegate):
    """Reject known-oversized dossier shards locally so existing 2->1 split owns recovery."""

    def guarded(api_key: str, prompt: str, model: str, expected_ids: list[str]):
        admitted, estimate = _routed_capacity_admitted(prompt)
        if not admitted:
            print(
                "Run122 final-payload admission split before provider call: "
                f"scope=dossier sections={','.join(expected_ids)} "
                f"estimated_total={estimate['estimated_request_tokens']} "
                f"limit={capacity.GROQ_FREE_TPM_LIMIT}"
            )
            raise dossier._DossierTransportPressure(
                "RUN122_FINAL_PAYLOAD_CAPACITY_PREFLIGHT "
                f"estimated_total={estimate['estimated_request_tokens']} "
                f"limit={capacity.GROQ_FREE_TPM_LIMIT}"
            )
        return delegate(api_key, prompt, model, expected_ids)

    guarded._isco_run122_final_payload_admission = True
    guarded._isco_run122_delegate = delegate
    return guarded


def install_run122_final_payload_admission() -> None:
    """Make all adaptive planning admission use the exact routed payload size.

    Writer/Doctor already own recursive 3->2+1->1 splitting. RepairDossier already
    owns bounded 2->1 splitting. This installer only feeds both owners the final
    post-middleware capacity verdict before any provider attempt is spent.
    """
    global _INSTALLED
    if _INSTALLED:
        return

    batching._capacity_admitted = _routed_capacity_admitted

    current = dossier._one_schema_bounded_call
    if not getattr(current, "_isco_run122_final_payload_admission", False):
        dossier._one_schema_bounded_call = _dossier_capacity_guard(current)

    _INSTALLED = True
    print(
        "Run122 final-payload admission installed: post-middleware Groq envelope; "
        "Writer/Doctor 3->2->1 and RepairDossier 2->1 split before known-oversized provider calls"
    )

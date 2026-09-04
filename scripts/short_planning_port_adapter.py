from __future__ import annotations

"""Compatibility adapter from Runner capacity transport to Engine ShortPlanningPort.

The existing capacity layer remains authoritative for prompt envelope admission,
provider routing and its single bounded terminal-reset retry. Engine owns the logical
Short Draft -> Review sequence and publishes named operation identity around the entire
transport call. This module deliberately contains no stage inference, provider policy or
retry policy of its own.
"""

import isco_video_agent.short_planning_port as short_port

from scripts import planning_capacity_headroom as headroom


_INSTALLED = False
_EXPECTED_PORT_CONTRACT = "engine.short_planning_port.v1"


class ShortPlanningPortCompositionError(RuntimeError):
    pass


def _engine_owned_short_build_plan(
    api_key: str,
    topic: str,
    content_model: str,
    *,
    research_context: dict | None,
    avoid_context: dict | None,
    revision_note: object,
):
    initial_prompt = headroom.build_short_initial_prompt(
        topic=topic,
        research_context=research_context,
        avoid_context=avoid_context,
        revision_note=revision_note,
    )

    def provider_call(prompt: str, *, phase: str) -> dict:
        # Capacity/retry remain Runner transport concerns. The Engine port surrounds
        # this complete call, so a retry cannot advance or lose the logical operation.
        headroom.certify_short_prompt_envelope(prompt, phase=phase)
        return headroom._short_provider_call_with_terminal_recovery(
            lambda: headroom.native_short.json_text(
                api_key,
                prompt,
                model=content_model,
            ),
            phase=phase,
        )

    def build_review_prompt(draft) -> str:
        return headroom.build_short_review_prompt(
            draft,
            research_context=research_context,
            revision_note=revision_note,
        )

    return short_port.execute_short_planning(
        initial_prompt=initial_prompt,
        build_review_prompt=build_review_prompt,
        provider_call=provider_call,
        parse_response=lambda data: headroom._parse_short_plan(data, topic),
    )


def install_short_planning_port_adapter() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    contract_id = getattr(short_port, "SHORT_PLANNING_PORT_CONTRACT_ID", None)
    if contract_id != _EXPECTED_PORT_CONTRACT:
        raise ShortPlanningPortCompositionError(
            "SHORT_PLANNING_PORT_CONTRACT_MISMATCH "
            f"expected={_EXPECTED_PORT_CONTRACT!r} observed={contract_id!r}"
        )
    if not getattr(headroom.native_short, "_ISCO_SHORT_INITIAL_ENVELOPE_V1", False):
        raise ShortPlanningPortCompositionError(
            "SHORT_PLANNING_PORT_INSTALL_ORDER capacity_headroom_not_installed"
        )

    # planning_capacity_headroom's bounded build wrapper resolves this module-global
    # function at call time. Replacing only the lifecycle executor converts that wrapper
    # into a compatibility adapter without disturbing its existing long-form fallback,
    # RepairDossier bypass, envelope admission or provider recovery behavior.
    headroom._build_short_plan = _engine_owned_short_build_plan
    _INSTALLED = True
    print(
        "Short Planning Port adapter installed: "
        f"contract={contract_id} lifecycle_owner=engine "
        "capacity_owner=runner transport_retry_owner=runner ordinal_inference=false"
    )

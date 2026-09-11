from __future__ import annotations

"""Bind legacy quality transports to the exact-model Capability Registry.

Several certified transports predate the Capability Router and still carry the dynamic
`openrouter/free` alias or live model discovery internally. Rewriting those transport
owners would duplicate mature schema/error logic. This binding changes only provider
selection: every release-critical OpenRouter model is sourced from the exact allowlist,
and alternate Vision recovery is deterministic inside that allowlist.
"""

from scripts import quality_capability_router as router


_INSTALLED = False


def _models(capability: str, provider: str) -> tuple[str, ...]:
    return tuple(
        candidate.model
        for candidate in router.policy_for(capability).candidates
        if candidate.provider == provider
    )


def _admitted_openrouter_vision_models(*, exclude: set[str]) -> tuple[str, ...]:
    decision = router.route_candidates(router.CAP_GOLD_VISION)
    return tuple(
        candidate.model
        for candidate in decision.candidates
        if candidate.provider == "openrouter" and candidate.model not in exclude
    )


def install_quality_capability_runtime_binding() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    from scripts import gold_final_critic_text_fallback as gold_text
    from scripts import vision_stage_contract_v2 as vision

    vision_models = _models(router.CAP_GOLD_VISION, "openrouter")
    text_models = _models(router.CAP_GOLD_TEXT, "openrouter")
    if len(vision_models) < 2:
        raise router.CapabilityRouteError(
            "gold_vision_requires_two_exact_openrouter_emergency_models"
        )
    if len(text_models) != 1:
        raise router.CapabilityRouteError(
            "gold_text_requires_one_exact_openrouter_emergency_model"
        )

    # Replace the dynamic aliases before any quality request can reach transport.
    vision.OPENROUTER_PRIMARY_MODEL = vision_models[0]
    gold_text._OPENROUTER_MODEL = text_models[0]

    def exact_alternate(*, exclude: set[str]) -> str | None:
        # Never query a catalog and select an arbitrary free model here. Preflight owns
        # exact-model availability; runtime health can still remove a pinned candidate.
        admitted = _admitted_openrouter_vision_models(exclude=set(exclude))
        return admitted[0] if admitted else None

    exact_alternate._isco_exact_quality_models_only = True
    vision._discover_alternate_free_vision_model = exact_alternate
    _INSTALLED = True


def reset_quality_capability_runtime_binding_for_tests() -> None:
    global _INSTALLED
    _INSTALLED = False

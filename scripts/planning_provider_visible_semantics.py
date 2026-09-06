from __future__ import annotations

"""Expose finite Planning semantic constraints before a provider attempt is spent.

Run #168 proved that the explicit Stage Contract could reject a provider response for
an allowed-value rule (`pillar`) that the provider itself was never told. That is a
contract-visibility bug: downstream validation was stricter than the provider-facing
contract. This installer keeps Stage Contract validation/cache authority unchanged and
adds one shared Long + Standalone-Short visibility seam:

- The canonical Planning pillar vocabulary is injected into the bound response schema
  so Groq/OpenRouter strict structured output cannot emit an unsupported value.
- The same finite vocabulary is appended to the model prompt so Gemini receives the
  exact same constraint even though it does not consume Runner's JSON schema.
- A final shared validator rejects an unsupported pillar before cache authority, so
  Long no longer silently normalizes a poisoned outline after Stage validation.

No provider routing, retry budget, capacity policy, Quality Gate, or editorial
selection policy changes here. The Engine's choose_pillar() remains a deterministic
local selector; this module merely makes its already-existing three-value codomain an
explicit provider contract.
"""

import copy
import functools
from dataclasses import replace

import isco_video_agent.resilient_planner as staged

from scripts import planning_stage_contract as stage_contract
from scripts.planner_quality_guard import _QUESTION_ANSWER_RUNTIME_RULE


PLANNING_PILLARS = ("understand", "rise", "see")
_VISIBLE_MARKER = "<PROVIDER_VISIBLE_SEMANTIC_CONTRACT>"
_INSTALLED = False

# Keep the richer local narrative-format contract as the semantic source of truth,
# but do not pay for its explanatory redundancy on every provider request. This is a
# deterministic transport projection used by both standalone preflight and canonical
# runtime. It retains the three acceptance facts that matter to the model: Q&A must be
# spoken (not metadata-only), answers are layered, and each exchange advances the
# argument rather than becoming a repetitive FAQ. Deterministic validators remain the
# authority after provider output.
_COMPACT_QUESTION_ANSWER_PROVIDER_RULE = (
    "Q&A: SPOKEN narration itself asks real questions with layered answers; "
    "do not collapse to metadata-only FAQ. Each exchange advances the argument."
)


def _compact_provider_prompt(prompt: str) -> str:
    return str(prompt).replace(
        _QUESTION_ANSWER_RUNTIME_RULE,
        _COMPACT_QUESTION_ANSWER_PROVIDER_RULE,
    )


def _has_pillar_field(owner: object) -> bool:
    schema = getattr(owner, "output_schema", None)
    if not isinstance(schema, dict):
        return False
    properties = schema.get("properties")
    return isinstance(properties, dict) and isinstance(properties.get("pillar"), dict)


def _with_pillar_schema(contract: stage_contract.PlanningStageContract) -> stage_contract.PlanningStageContract:
    if not _has_pillar_field(contract):
        return contract
    schema = copy.deepcopy(contract.output_schema)
    schema["properties"]["pillar"] = {
        "type": "string",
        "enum": list(PLANNING_PILLARS),
    }
    rules = dict(contract.semantic_rules)
    rules["allowed_pillars"] = list(PLANNING_PILLARS)
    return replace(contract, output_schema=schema, semantic_rules=rules)


def _provider_visible_prompt(prompt: str, owner: object | None) -> str:
    prompt = _compact_provider_prompt(prompt)
    if owner is None or not _has_pillar_field(owner) or _VISIBLE_MARKER in prompt:
        return prompt
    values = " | ".join(PLANNING_PILLARS)
    return (
        prompt
        + "\n\n"
        + _VISIBLE_MARKER
        + "\n`pillar` MUST be exactly: "
        + values
        + "; do not translate/rename/invent."
    )


def install_planning_provider_visible_semantics() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    # Bind the finite value set into the authoritative request contract. The original
    # StageSpec remains stage identity only; provider schema/cache fingerprint are
    # request-bound and therefore receive this visibility upgrade in one place.
    current_bind = stage_contract.bind_request_contract
    if not getattr(current_bind, "_isco_provider_visible_semantics_v1", False):
        @functools.wraps(current_bind)
        def bind_request_contract(spec, effective_prompt):
            return _with_pillar_schema(current_bind(spec, effective_prompt))

        bind_request_contract._isco_provider_visible_semantics_v1 = True
        bind_request_contract._isco_original = current_bind
        stage_contract.bind_request_contract = bind_request_contract

    # Preserve every existing structural/semantic validator (including native Short's
    # stronger topic/format/duration checks), then add the one shared finite-value rule
    # that Long historically normalized only after Stage validation.
    current_validate = stage_contract.validate_response
    if not getattr(current_validate, "_isco_provider_visible_semantics_v1", False):
        @functools.wraps(current_validate)
        def validate_response(contract, data):
            result = current_validate(contract, data)
            if _has_pillar_field(contract):
                pillar = str(data.get("pillar") or "").strip()
                if pillar not in PLANNING_PILLARS:
                    stage_contract._raise_validation(
                        stage_contract.PlanningErrorCode.SEMANTIC_INVALID,
                        contract,
                        "$.pillar",
                        "unsupported_pillar",
                    )
            return result

        validate_response._isco_provider_visible_semantics_v1 = True
        validate_response._isco_original = current_validate
        stage_contract.validate_response = validate_response

    # Gemini does not consume Runner's JSON schema. Add the same finite constraint to
    # the effective prompt before the explicit Stage router hashes/admission-checks it.
    # This wrapper is intentionally outside, not instead of, the Stage router and carries
    # its marker so lifecycle reassertion cannot mistake it for a legacy router.
    current_json_text = staged.json_text
    if not getattr(current_json_text, "_isco_provider_visible_semantics_v1", False):
        @functools.wraps(current_json_text)
        def provider_visible_json_text(api_key, prompt, model="gemini-2.5-flash"):
            spec = stage_contract._ACTIVE_STAGE_SPEC.get()
            return current_json_text(
                api_key,
                _provider_visible_prompt(prompt, spec),
                model=model,
            )

        provider_visible_json_text._isco_provider_visible_semantics_v1 = True
        provider_visible_json_text._isco_original = current_json_text
        setattr(provider_visible_json_text, stage_contract._ROUTER_MARKER, True)
        staged.json_text = provider_visible_json_text

    _INSTALLED = True
    print(
        "Planning provider-visible semantics installed: "
        "pillar=understand|rise|see scope=Long+StandaloneShort schema+prompt+validation "
        "question_answer_transport=compact_shared_projection"
    )

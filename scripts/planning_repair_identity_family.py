from __future__ import annotations

"""Server-owned identity contract for Planning repair transports.

A repair model edits bounded content; it never owns execution identity. Standalone
Moment historically reused the full Draft/Review response schema during repair, which
made the provider echo ``topic`` and ``format`` even though the host immediately
rebound those values when rebuilding the plan. A transient retry could therefore return
perfectly usable mutable content with a paraphrased topic and fail *before* the host
rebind happened.

This family makes the ownership boundary explicit:

* Short repair returns a strict mutable patch with no ``topic``/``format`` fields.
* The host injects the already-approved identity only for semantic validation and later
  reconstructs the plan through the existing Engine-owned ``_plan_from_dict`` path.
* Long dossier repair is already a section-only patch. We certify that invariant here so
  a future regression cannot silently give a Long repair plan-level identity authority.

No provider order, retry budget, capacity limit, quality gate, approved-input check, or
Draft/Review contract is changed.
"""

import copy
import functools
from dataclasses import replace

from scripts import native_short_stage_contract as short_stage
from scripts import planning_stage_contract as stage_contract
from scripts import short_planning_repair


IDENTITY_MODE = "server_owned_repair_patch"
IMMUTABLE_IDENTITY_FIELDS = ("topic", "format")
SHORT_REPAIR_CONTRACT_ID = "planning.short_repair.v2"
_INSTALLED = False


def _repair_patch_schema() -> dict:
    schema = copy.deepcopy(short_stage._moment_schema())
    properties = schema.get("properties")
    required = schema.get("required")
    if not isinstance(properties, dict) or not isinstance(required, list):
        raise stage_contract.PlanningStageError(
            stage_contract.PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
            "native Short repair could not derive the canonical Moment schema",
            stage_id="planning.short_repair",
        )
    for field in IMMUTABLE_IDENTITY_FIELDS:
        properties.pop(field, None)
    schema["required"] = [field for field in required if field not in IMMUTABLE_IDENTITY_FIELDS]
    return schema


def _repair_patch_spec(spec: stage_contract.PlanningStageSpec) -> stage_contract.PlanningStageSpec:
    rules = dict(spec.semantic_rules)
    rules.update(
        {
            "identity_mode": IDENTITY_MODE,
            "immutable_identity_fields": list(IMMUTABLE_IDENTITY_FIELDS),
        }
    )
    return replace(
        spec,
        contract_id=SHORT_REPAIR_CONTRACT_ID,
        output_schema=_repair_patch_schema(),
        semantic_rules=rules,
    )


def _rewrite_short_repair_prompt(prompt: str) -> str:
    """Make the provider-facing repair request match the strict patch schema.

    Exact markers are intentional. If the upstream repair prompt changes, fail closed
    rather than silently reintroducing a full-plan repair contract.
    """
    text = str(prompt)
    replacements = (
        (
            "Return one complete corrected plan using EXACTLY the same JSON schema as CURRENT_MOMENT.",
            "Return only a mutable repair patch for the EXISTING plan. `topic` and `format` are host-owned immutable identity: read them from CURRENT_MOMENT for context, but never return or rewrite them.",
        ),
        (
            "- format stays moment and sections stays EXACTLY one section.",
            "- Host-owned format stays moment. Do not return or rewrite `topic` or `format`; sections stays EXACTLY one section.",
        ),
        (
            "- Keep duration 7-20 seconds and max two short on-screen lines.",
            "- Keep duration 12-20 seconds and max two short on-screen lines.",
        ),
        (
            "- Preserve the approved topic and the useful promise unless a blocking issue requires a wording correction.",
            "- The approved topic is immutable read-only context. Preserve the useful promise inside mutable copy; never rewrite the topic.",
        ),
        (
            "Return JSON only with keys: topic,pillar,format,hook,title_options,thumbnail_concepts,sections,cta,closing_payoff.",
            "Return JSON only with mutable keys: pillar,hook,title_options,thumbnail_concepts,sections,cta,closing_payoff. Do not return `topic` or `format`; they are host-owned immutable identity.",
        ),
    )
    for old, new in replacements:
        if old not in text:
            raise short_planning_repair.ShortRepairEnvelopeError(
                "Short repair identity contract could not bind the provider prompt; expected marker missing"
            )
        text = text.replace(old, new, 1)
    return text


def _validate_short_repair_patch(
    contract: stage_contract.PlanningStageContract,
    data: dict,
) -> dict:
    # Validate exactly what the provider owns first. additionalProperties=False makes
    # any attempted identity echo/change a structural failure rather than a value we
    # silently trust or normalize.
    stage_contract._validate_schema(data, contract.output_schema, contract)

    # Reconstruct a validation-only full view from server-owned identity. This mirrors
    # the Engine's later _plan_from_dict(data, approved_topic, "moment", ...) ownership
    # without changing provider output or cache payload bytes.
    semantic_view = dict(data)
    semantic_view["topic"] = str(contract.semantic_rules.get("approved_topic") or "")
    semantic_view["format"] = str(contract.semantic_rules.get("format") or "")
    short_stage._validate_short_semantics(contract, semantic_view)
    return data


def _assert_repair_identity_family() -> None:
    short_schema = _repair_patch_schema()
    short_props = set((short_schema.get("properties") or {}).keys())
    leaked_short = sorted(short_props.intersection(IMMUTABLE_IDENTITY_FIELDS))
    if leaked_short:
        raise stage_contract.PlanningStageError(
            stage_contract.PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
            "Short repair schema leaked host-owned identity: " + ",".join(leaked_short),
            stage_id="planning.short_repair",
        )

    # Long already uses the professional patch boundary: the provider owns only exact
    # section fragments. Certify it here rather than changing a path that is already
    # correct and battle-tested.
    long_spec = stage_contract.script_stage_spec("dossier_repair", ["identity-family-probe"])
    long_props = set((long_spec.output_schema.get("properties") or {}).keys())
    if long_props != {"sections"}:
        raise stage_contract.PlanningStageError(
            stage_contract.PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
            "Long dossier repair gained unexpected plan-level output authority: "
            + ",".join(sorted(long_props)),
            stage_id="planning.dossier_repair",
        )


def install_planning_repair_identity_family() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    _assert_repair_identity_family()

    current_spec = short_stage.moment_stage_spec
    if not getattr(current_spec, "_isco_repair_identity_family", False):
        @functools.wraps(current_spec)
        def moment_stage_spec(stage_kind: str, topic: str):
            spec = current_spec(stage_kind, topic)
            if stage_kind != "short_repair":
                return spec
            return _repair_patch_spec(spec)

        moment_stage_spec._isco_repair_identity_family = True
        moment_stage_spec._isco_original = current_spec
        short_stage.moment_stage_spec = moment_stage_spec

    current_prompt = short_planning_repair.build_short_repair_prompt
    if not getattr(current_prompt, "_isco_repair_identity_family", False):
        @functools.wraps(current_prompt)
        def build_short_repair_prompt(*args, **kwargs):
            return _rewrite_short_repair_prompt(current_prompt(*args, **kwargs))

        build_short_repair_prompt._isco_repair_identity_family = True
        build_short_repair_prompt._isco_original = current_prompt
        short_planning_repair.build_short_repair_prompt = build_short_repair_prompt

    current_validate = stage_contract.validate_response
    if not getattr(current_validate, "_isco_repair_identity_family", False):
        @functools.wraps(current_validate)
        def validate_response(contract, data):
            if str(contract.semantic_rules.get("identity_mode") or "") == IDENTITY_MODE:
                if not isinstance(data, dict):
                    stage_contract._raise_validation(
                        stage_contract.PlanningErrorCode.STRUCTURAL_INVALID,
                        contract,
                        "$",
                        "expected_object",
                    )
                return _validate_short_repair_patch(contract, data)
            return current_validate(contract, data)

        validate_response._isco_repair_identity_family = True
        validate_response._isco_original = current_validate
        stage_contract.validate_response = validate_response

    _INSTALLED = True
    print(
        "Planning repair identity family installed: "
        "Short=server_owned_topic_format_patch Long=certified_section_patch "
        "DraftReview=unchanged retry_budgets=unchanged quality_gates=unchanged"
    )

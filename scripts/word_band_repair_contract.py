from __future__ import annotations

"""Single-owner Film word-band preservation and local-repair context contract.

This module adds no provider, retry, or quality-gate owner. It composes the existing
append_retry_guard with two deterministic policies:
1) Script Doctor may improve Film sections, but it may not turn a section that was
   already inside the hard 110-170 word band into a new word-band defect.
2) Residual append repair receives only the minimum sufficient editorial context.
   The existing Planning Stage Contract remains the sole retry/capacity owner.
"""

import json
from typing import Any

import isco_video_agent.repair_dossier as repair_dossier
import isco_video_agent.resilient_planner as staged
from scripts import append_retry_guard as append_guard


_REPAIR_MARKER = "_isco_word_band_projected_context"
_DOCTOR_MARKER = "_isco_word_band_doctor_preservation"

_LOCAL_REPAIR_RULE = (
    "Do not introduce any new externally verifiable factual, medical, scientific, "
    "legal, or religious claim. Deepen only the target's existing idea using wording, "
    "reasoning, consequences, distinctions, or everyday examples already supported by "
    "the current narration and canonical editorial intent."
)


def _json_object(raw: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{label} must be valid JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must be a JSON object")
    return value


def _project_policy_json(raw: str) -> str:
    """Keep only text-repair policy; drop visual/audio/brand payloads.

    Values and language rules are hard cultural/editorial constraints, so they are
    preserved exactly. Visual, audio and brand-signature policy cannot affect an
    append-only narration continuation and are intentionally not sent to the model.
    """

    source = _json_object(raw, "EDITORIAL_POLICY")
    payload: dict[str, Any] = {}
    for key in ("audience", "positioning", "language", "values", "release_gate"):
        if key in source:
            payload[key] = source[key]
    payload["local_repair_rule"] = _LOCAL_REPAIR_RULE
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _project_research_json(raw: str) -> str:
    """Project research to hard boundaries only; append repair may not add new facts.

    The full approved research pack and market signals are useful while authoring the
    script, but not for a local append-only repair whose contract explicitly forbids
    introducing new factual claims. Keeping them here made a tiny residual defect carry
    almost a full planning envelope and could make free-tier Groq impossible to admit.
    """

    source = _json_object(raw, "RESEARCH_DATA")
    payload: dict[str, Any] = {
        "repair_factuality_rule": _LOCAL_REPAIR_RULE,
    }
    for key in ("content_boundaries", "factuality_rule"):
        if key in source:
            payload[key] = source[key]
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _strip_host_identity(text: str, opener: str, closer: str) -> str:
    result = str(text or "").strip()
    strip_fn = getattr(staged, "_strip_exact_host_phrase", None)
    if callable(strip_fn):
        result = strip_fn(result, opener)
        result = strip_fn(result, closer)
    return str(result or "").strip()


def _install_script_doctor_band_preservation() -> None:
    current = staged._script_doctor
    if getattr(current, _DOCTOR_MARKER, False):
        return

    original = current
    section_minimum = int(repair_dossier.FILM_SECTION_MIN_WORDS)
    section_maximum = int(repair_dossier.FILM_SECTION_MAX_WORDS)
    film_count = int(getattr(staged, "_SECTION_COUNTS", {}).get("film", 8))

    def guarded_script_doctor(*args, **kwargs):
        sections = kwargs.get("sections")
        if sections is None and len(args) > 3:
            sections = args[3]

        # Script Doctor serves Film and Story. Only Film owns the 110-170 contract.
        if not isinstance(sections, list) or len(sections) != film_count:
            return original(*args, **kwargs)

        opener = str(kwargs.get("identity_opener", "") or "")
        closer = str(kwargs.get("identity_closer", "") or "")
        baseline: dict[str, tuple[str, int]] = {}
        for section in sections:
            narration = _strip_host_identity(section.narration, opener, closer)
            baseline[str(section.id)] = (narration, staged._word_count(narration))

        corrected = original(*args, **kwargs)
        if not isinstance(corrected, dict):
            raise RuntimeError("Script Doctor must return a section mapping")

        restored: list[str] = []
        for section_id, (baseline_narration, baseline_words) in baseline.items():
            if not section_minimum <= baseline_words <= section_maximum:
                # Existing defects are still the Doctor/local-repair system's job.
                continue
            entry = corrected.get(section_id)
            if not isinstance(entry, dict):
                raise RuntimeError(f"Script Doctor omitted section {section_id}")
            candidate = _strip_host_identity(
                str(entry.get("narration", "") or ""),
                opener,
                closer,
            )
            candidate_words = staged._word_count(candidate)
            if section_minimum <= candidate_words <= section_maximum:
                continue

            # Do not spend another provider call. Restore the already-valid narration;
            # the Doctor may still keep a corrected key_point or other schema fields.
            replacement = dict(entry)
            replacement["narration"] = baseline_narration
            corrected[section_id] = replacement
            restored.append(
                f"{section_id}:{baseline_words}->{candidate_words}"
            )

        if restored:
            print(
                "Film Script Doctor word-band preservation: restored="
                + ",".join(restored)
                + " provider_calls_added=0 hard_band="
                + f"{section_minimum}-{section_maximum}"
            )
        return corrected

    setattr(guarded_script_doctor, _DOCTOR_MARKER, True)
    staged._script_doctor = guarded_script_doctor


def _install_projected_append_context() -> None:
    current = append_guard._repair_all_residual_underlength
    if getattr(current, _REPAIR_MARKER, False):
        staged._script_doctor_underlength_retry = current
        return

    original = current

    def projected_repair(
        api_key: str,
        *,
        topic: str,
        model: str,
        sections: list,
        policy_json: str,
        research_json: str,
        narrative_format: str,
        current_words: int,
        minimum: int,
        editorial_intent_json: str = "",
    ) -> dict[str, str]:
        return original(
            api_key,
            topic=topic,
            model=model,
            sections=sections,
            policy_json=_project_policy_json(policy_json),
            research_json=_project_research_json(research_json),
            narrative_format=narrative_format,
            current_words=current_words,
            minimum=minimum,
            editorial_intent_json=editorial_intent_json,
        )

    setattr(projected_repair, _REPAIR_MARKER, True)

    # The existing post-build residual guard resolves this module global at call time,
    # while Engine's internal underlength path holds the staged function reference.
    # Bind both to one exact repair owner; no extra provider call or retry is created.
    append_guard._repair_all_residual_underlength = projected_repair
    staged._script_doctor_underlength_retry = projected_repair


def install_word_band_repair_contract() -> None:
    """Install deterministic Film band preservation + compact local repair context."""

    _install_script_doctor_band_preservation()
    _install_projected_append_context()
    print(
        "Word-band repair contract installed: Film Script Doctor cannot create new "
        "110-170 defects from previously valid sections; residual append repair uses "
        "projected text-policy/factuality context; provider order, retry ownership, "
        "800-1450 aggregate gate, Stage Contract, and final quality gates unchanged"
    )

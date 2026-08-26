from __future__ import annotations

import json

import isco_video_agent.resilient_planner as staged


_MARKER = "_isco_schema_repair_policy"
_SCHEMA_ERROR_MARKERS = (
    "Full script must contain exactly",
    "Full script section entry must be an object",
    "is missing id or narration",
    "Full script duplicated section id",
    "Full script must return the exact section ids in order",
)
_SCRIPT_DOCTOR_MARKER = "senior Arabic script editor and cultural QA reviewer"
_SCRIPT_DOCTOR_SECTIONS_MARKER = "SECTIONS:"


def _is_local_output_schema_error(exc: Exception) -> bool:
    detail = str(exc)
    return any(marker in detail for marker in _SCHEMA_ERROR_MARKERS)


def _valid_partial_sections(data: dict, expected_ids: list[str]) -> dict[str, dict]:
    """Keep only usable provider edits without pretending an incomplete response is complete."""
    sections = data.get("sections")
    if not isinstance(sections, list):
        return {}
    allowed = set(expected_ids)
    by_id: dict[str, dict] = {}
    for item in sections:
        if not isinstance(item, dict):
            continue
        section_id = str(item.get("id", "")).strip()
        narration = str(item.get("narration", "")).strip()
        if section_id not in allowed or not narration or section_id in by_id:
            continue
        by_id[section_id] = {
            "narration": narration,
            "key_point": str(item.get("key_point", "")).strip(),
        }
    return by_id


def _json_after_marker(prompt: str, marker: str):
    index = prompt.rfind(marker)
    if index < 0:
        return None
    tail = prompt[index + len(marker) :].lstrip()
    try:
        value, _ = json.JSONDecoder().raw_decode(tail)
    except json.JSONDecodeError:
        return None
    return value


def _between(prompt: str, start_marker: str, end_marker: str) -> str:
    start = prompt.find(start_marker)
    if start < 0:
        return ""
    start += len(start_marker)
    end = prompt.find(end_marker, start)
    if end < 0:
        return ""
    return prompt[start:end].strip()


def _compact_script_doctor_repair(
    prompt: str,
    data: dict,
    expected_ids: list[str],
) -> tuple[str, dict[str, dict], list[str]] | None:
    """Build a small missing-section completion request for a partial Script Doctor response.

    Run #114 showed that replaying the entire long Script Doctor prompt after receiving
    a mostly-valid response amplifies provider pressure exactly when capacity is already
    degraded. The original pre-doctor narration is embedded in this prompt, so a bounded
    repair can ask only for omitted/invalid section ids while preserving the first call's
    valid edits. This does not invent missing content locally and it does not weaken any
    downstream quality/duration gate.
    """
    if _SCRIPT_DOCTOR_MARKER not in prompt or _SCRIPT_DOCTOR_SECTIONS_MARKER not in prompt:
        return None

    partial = _valid_partial_sections(data, expected_ids)
    missing_ids = [section_id for section_id in expected_ids if section_id not in partial]
    if not partial or not missing_ids:
        return None

    originals = _json_after_marker(prompt, _SCRIPT_DOCTOR_SECTIONS_MARKER)
    if not isinstance(originals, list):
        return None
    original_by_id: dict[str, dict] = {}
    for item in originals:
        if not isinstance(item, dict):
            continue
        section_id = str(item.get("id", "")).strip()
        if section_id in expected_ids and section_id not in original_by_id:
            original_by_id[section_id] = item
    if any(section_id not in original_by_id for section_id in missing_ids):
        return None

    missing_originals = [original_by_id[section_id] for section_id in missing_ids]
    corrected_context = [
        {"id": section_id, "key_point": partial[section_id]["key_point"]}
        for section_id in expected_ids
        if section_id in partial
    ]
    editorial_intent = _json_after_marker(
        prompt,
        "CANONICAL EDITORIAL_INTENT (immutable during repair):",
    )
    issue_notes = _between(
        prompt,
        "Specific issues an automated pre-check found that you MUST address:",
        "EDITORIAL_POLICY:",
    )

    repair_prompt = f"""
You are completing ONE bounded schema-recovery step for a senior Arabic Script Doctor.
The previous response already returned usable corrected entries for some sections, but omitted or invalidated the ids
listed below. Do NOT rewrite or repeat the already-valid sections. Return corrections ONLY for the missing ids.

MISSING_IDS_IN_EXACT_ORDER:
{json.dumps(missing_ids, ensure_ascii=False)}

ORIGINAL_MISSING_SECTIONS:
{json.dumps(missing_originals, ensure_ascii=False)}

ALREADY_CORRECTED_SECTION_KEY_POINTS (context only; do not return these ids):
{json.dumps(corrected_context, ensure_ascii=False)}

CANONICAL_EDITORIAL_INTENT (immutable):
{json.dumps(editorial_intent or {}, ensure_ascii=False)}

AUTOMATED_ISSUES_FROM_THE_PARENT_REVIEW:
{issue_notes[:5000] or "No additional issue text was recoverable; preserve the original meaning and improve only what is necessary."}

Hard constraints:
- Preserve the original section purpose, factual boundaries, and canonical editorial intent.
- Use natural contemporary Modern Standard Arabic suitable for spoken narration.
- Correct the applicable issue notes without adding filler, unsupported facts, medical claims, religious quotations, or new attributions.
- Do not create, quote, paraphrase, imitate, or invent a channel opener, farewell, sign-off, outro, or second ending.
- Give each returned section one distinct key_point.
- Return each missing id exactly once and in the exact order shown above.
- This is the one schema-recovery call. Return a complete valid JSON object and nothing else.

Return ONLY JSON: {{"sections": [{{"id": "...", "narration": "...", "key_point": "..."}}, ...]}} with EXACTLY
{len(missing_ids)} entries.
"""
    return repair_prompt, partial, missing_ids


def _merge_partial_and_repair(
    partial: dict[str, dict],
    repaired: dict[str, dict],
    expected_ids: list[str],
) -> dict[str, dict]:
    merged = dict(partial)
    merged.update(repaired)
    payload = {
        "sections": [
            {"id": section_id, **merged[section_id]}
            for section_id in expected_ids
            if section_id in merged
        ]
    }
    return staged._parse_full_script_response(payload, expected_ids)


def install_schema_repair_policy() -> None:
    """Keep schema repair owned by the schema layer, not the provider layer.

    The Engine's historical helper catches every exception, which can replay an entire
    provider-router sequence after rate-limit/network/auth/budget failures. Production
    already has one provider router/fallback owner. This replacement retries exactly
    once only after a provider returned a JSON object that failed the local full-script
    shape/id/order contract. For a partial Script Doctor response, that one retry is a
    compact missing-section completion instead of a full long-prompt replay.
    """
    current = staged._call_with_schema_repair
    if getattr(current, _MARKER, False):
        return

    def bounded_schema_call(
        api_key: str,
        prompt: str,
        model: str,
        *,
        expected_ids: list[str],
    ):
        data = staged.json_text(api_key, prompt, model=model)
        try:
            return staged._parse_full_script_response(data, expected_ids)
        except Exception as exc:
            if not _is_local_output_schema_error(exc):
                raise

            compact = _compact_script_doctor_repair(prompt, data, expected_ids)
            if compact is not None:
                compact_prompt, partial, missing_ids = compact
                repair_data = staged.json_text(api_key, compact_prompt, model=model)
                repaired = staged._parse_full_script_response(repair_data, missing_ids)
                result = _merge_partial_and_repair(partial, repaired, expected_ids)
                print(
                    "Schema repair completed partial Script Doctor output with one compact reask: "
                    f"kept={len(partial)} repaired={len(missing_ids)} total={len(expected_ids)}"
                )
                return result

            repair_prompt = prompt + staged._SCHEMA_REPAIR_SUFFIX.format(count=len(expected_ids))
            repair_data = staged.json_text(api_key, repair_prompt, model=model)
            return staged._parse_full_script_response(repair_data, expected_ids)

    setattr(bounded_schema_call, _MARKER, True)
    staged._call_with_schema_repair = bounded_schema_call
    print(
        "Schema repair policy installed: one local shape/id/order recovery only; "
        "partial Script Doctor replies use compact missing-section completion; "
        "provider/router/auth/network/budget failures are never replayed as schema repair"
    )

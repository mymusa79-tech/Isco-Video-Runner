from __future__ import annotations

from typing import Any

import scripts.append_retry_guard as append_guard
from scripts.attempt10_append_bound_recovery import install_attempt10_append_bound_recovery


_MARKER = "_isco_attempt9_schema_normalizer"


def _normalized_mapping(mapping: dict[Any, Any], expected_ids: list[str]) -> list[dict[str, Any]]:
    """Convert only mechanically equivalent id->append mappings into list entries.

    This deliberately does not repair semantic mistakes. Unknown ids, empty values,
    duplicate normalized keys, and ambiguous nested objects still fail closed.
    Existing append_retry_guard validation remains the authority for ordering,
    target completeness, section bounds, and aggregate headroom.
    """
    if not mapping:
        return []

    # A provider sometimes emits one addition object where the contract asked for a
    # one-element list. Preserve the object verbatim so the existing parser validates
    # its id/text and, on the aggregate-underlength path, decides whether completion
    # of other targets is allowed.
    if "id" in mapping or "append_text" in mapping:
        if "id" not in mapping or "append_text" not in mapping:
            raise RuntimeError("Append-only retry single-object schema is incomplete")
        return [dict(mapping)]

    expected_set = set(expected_ids)
    normalized: dict[str, Any] = {}
    for raw_id, value in mapping.items():
        section_id = str(raw_id).strip()
        if not section_id or section_id in normalized:
            raise RuntimeError("Append-only retry mapping contains an empty or duplicate normalized id")
        if section_id not in expected_set:
            raise RuntimeError(f"Append-only retry mapping returned unexpected section id: {section_id}")
        normalized[section_id] = value

    items: list[dict[str, Any]] = []
    for section_id in expected_ids:
        if section_id not in normalized:
            continue
        value = normalized[section_id]
        if isinstance(value, str):
            append_text: Any = value
        elif isinstance(value, dict):
            nested_id = value.get("id")
            if nested_id is not None and str(nested_id).strip() != section_id:
                raise RuntimeError(
                    f"Append-only retry mapping id mismatch for section {section_id}"
                )
            if "append_text" not in value:
                raise RuntimeError(
                    f"Append-only retry mapping object for {section_id} is missing append_text"
                )
            append_text = value.get("append_text")
        else:
            raise RuntimeError(
                f"Append-only retry mapping value for {section_id} must be text or an object"
            )
        items.append({"id": section_id, "append_text": append_text})
    return items


def _normalize_additions_payload(data: Any, expected_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Normalize a narrow set of JSON-equivalent provider shapes without another LLM call."""
    shape = "canonical_object"

    if isinstance(data, list):
        additions = list(data)
        shape = "top_level_list"
    elif isinstance(data, dict):
        if "additions" in data:
            raw_additions = data.get("additions")
            if isinstance(raw_additions, list):
                return data
            if isinstance(raw_additions, dict):
                additions = _normalized_mapping(raw_additions, expected_ids)
                shape = "additions_object"
            else:
                raise RuntimeError(
                    "Append-only retry additions has unsupported schema type: "
                    + type(raw_additions).__name__
                )
        else:
            # Accept only an unmistakable top-level addition object or an id-keyed
            # mapping whose keys are all expected targets. Arbitrary wrappers such as
            # {data: ...} remain rejected rather than guessed through.
            additions = _normalized_mapping(data, expected_ids)
            shape = "top_level_object"
    else:
        raise RuntimeError(
            "Append-only retry response has unsupported top-level schema type: "
            + type(data).__name__
        )

    ids = [
        str(item.get("id", "")).strip()
        for item in additions
        if isinstance(item, dict)
    ]
    print(
        "Append-only retry schema normalization: "
        f"shape={shape} entries={len(additions)} ids={ids}"
    )
    return {"additions": additions}


def install_attempt9_schema_normalizer() -> None:
    """Harden append-retry schema handling while preserving every semantic hard gate."""
    # Attempt 10 extends only the already-existing aggregate-underlength completion
    # mechanism: discard a first-pass bound-invalid target and spend the same single
    # completion call. Installing it here keeps run_v3's proven installer order intact.
    install_attempt10_append_bound_recovery()

    current_subset = append_guard._parse_ordered_subset_for_schema_completion
    current_complete = append_guard._parse_safe_partial_additions
    if getattr(current_subset, _MARKER, False) and getattr(current_complete, _MARKER, False):
        return

    original_subset = current_subset
    original_complete = current_complete

    def normalized_subset(data, expected_ids):
        normalized = _normalize_additions_payload(data, expected_ids)
        return original_subset(normalized, expected_ids)

    def normalized_complete(data, expected_ids):
        normalized = _normalize_additions_payload(data, expected_ids)
        return original_complete(normalized, expected_ids)

    setattr(normalized_subset, _MARKER, True)
    setattr(normalized_complete, _MARKER, True)
    append_guard._parse_ordered_subset_for_schema_completion = normalized_subset
    append_guard._parse_safe_partial_additions = normalized_complete

    # If the base append guard was already installed, keep the Engine-side parser in
    # sync too. Normal production installs this normalizer first, so this is merely an
    # idempotent safety path for tests and future integration order changes.
    if append_guard.staged._parse_append_only_response is original_complete:
        append_guard.staged._parse_append_only_response = normalized_complete

    print(
        "Attempt 9 append schema normalizer installed: canonical list plus narrowly "
        "equivalent list/object/id-map shapes; semantic validation and provider-call "
        "limits unchanged"
    )

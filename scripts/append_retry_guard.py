from __future__ import annotations

from functools import wraps

import isco_video_agent.resilient_planner as staged


def _parse_safe_partial_additions(data: dict, expected_ids: list[str]) -> dict[str, str]:
    """Accept a non-empty safe subset of append-only additions.

    The model is still asked for every target, but a single provider call may
    under-return one valid addition. We never synthesize missing content and never
    accept replacement narration. The Engine's existing post-append duration gate
    remains the authority on whether the actual returned text is sufficient.
    """
    additions = data.get("additions")
    if isinstance(additions, dict):
        additions = [additions]
    if not isinstance(additions, list) or not additions or len(additions) > len(expected_ids):
        raise RuntimeError(
            f"Append-only retry must contain between 1 and {len(expected_ids)} allowed additions"
        )

    allowed = set(expected_ids)
    by_id: dict[str, str] = {}
    returned_ids: list[str] = []
    for item in additions:
        if not isinstance(item, dict):
            raise RuntimeError("Append-only retry addition must be an object")
        section_id = str(item.get("id", "")).strip()
        append_text = str(item.get("append_text", "")).strip()
        if not section_id or not append_text:
            raise RuntimeError(f"Append-only retry addition '{section_id}' is missing id or append_text")
        if section_id not in allowed:
            raise RuntimeError(f"Append-only retry referenced non-target section: {section_id}")
        if section_id in by_id:
            raise RuntimeError(f"Append-only retry duplicated section id: {section_id}")
        returned_ids.append(section_id)
        by_id[section_id] = append_text

    expected_subset_order = [section_id for section_id in expected_ids if section_id in by_id]
    if returned_ids != expected_subset_order:
        raise RuntimeError(
            "Append-only retry additions must preserve target order: " + ", ".join(expected_ids)
        )
    return by_id


def install_append_retry_guard() -> None:
    """Patch only the bounded append-only response parser used by production."""
    current_retry = staged._script_doctor_underlength_retry
    if not getattr(current_retry, "_isco_append_retry_trace", False):
        @wraps(current_retry)
        def traced_retry(*args, **kwargs):
            print("PLANNING_BOUNDARY ENTER append_retry_guard")
            try:
                result = current_retry(*args, **kwargs)
            except Exception as exc:
                detail = str(exc).replace("\n", " ")[:220]
                print(
                    "PLANNING_BOUNDARY ERROR append_retry_guard "
                    + f"type={type(exc).__name__} detail={detail}"
                )
                raise
            print("PLANNING_BOUNDARY EXIT append_retry_guard")
            return result

        traced_retry._isco_append_retry_trace = True
        staged._script_doctor_underlength_retry = traced_retry

    staged._parse_append_only_response = _parse_safe_partial_additions
    print("Append-only retry guard installed: safe partial additions allowed; hard duration gate unchanged")

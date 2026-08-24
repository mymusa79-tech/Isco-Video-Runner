from __future__ import annotations

import isco_video_agent.resilient_planner as staged


_MARKER = "_isco_schema_repair_policy"
_SCHEMA_ERROR_MARKERS = (
    "Full script must contain exactly",
    "Full script section entry must be an object",
    "is missing id or narration",
    "Full script duplicated section id",
    "Full script must return the exact section ids in order",
)


def _is_local_output_schema_error(exc: Exception) -> bool:
    detail = str(exc)
    return any(marker in detail for marker in _SCHEMA_ERROR_MARKERS)


def install_schema_repair_policy() -> None:
    """Keep schema repair owned by the schema layer, not the provider layer.

    The Engine's historical helper catches every exception, which can replay an entire
    provider-router sequence after rate-limit/network/auth/budget failures. Production
    already has one provider router/fallback owner. This replacement retries exactly
    once only after a provider returned a JSON object that failed the local full-script
    shape/id/order contract.
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
            repair_prompt = prompt + staged._SCHEMA_REPAIR_SUFFIX.format(count=len(expected_ids))
            repair_data = staged.json_text(api_key, repair_prompt, model=model)
            return staged._parse_full_script_response(repair_data, expected_ids)

    setattr(bounded_schema_call, _MARKER, True)
    staged._call_with_schema_repair = bounded_schema_call
    print(
        "Schema repair policy installed: exactly one local shape/id/order reask; "
        "provider/router/auth/network/budget failures are never replayed as schema repair"
    )

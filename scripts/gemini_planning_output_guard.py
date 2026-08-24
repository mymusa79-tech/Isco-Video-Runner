from __future__ import annotations

from typing import Callable

import isco_video_agent.providers.gemini as gemini_provider
import scripts.task_level_planner_router as planner_router


_MARKER = "_isco_gemini_planning_output_guard"
_JSON_OBJECT_SCHEMA = {
    "type": "object",
    "additionalProperties": True,
}
_JSON_ONLY_SUFFIX = "\nReturn ONLY one complete valid JSON object. No markdown fences or commentary."


def _status_value(value: object) -> str:
    if value is None:
        return ""
    raw = getattr(value, "value", value)
    return str(raw).strip().lower()


def _guarded_gemini_json_text(
    api_key: str,
    prompt: str,
    model: str = "gemini-2.5-flash",
    *,
    max_output_tokens: int | None = None,
    raw_observer: Callable[[str], None] | None = None,
) -> dict:
    """Planning-only Gemini adapter with native JSON mode and interaction status checks."""
    client = gemini_provider._client(api_key)
    enriched = gemini_provider.with_channel_persona(prompt)
    kwargs: dict = {
        "response_format": {
            "type": "text",
            "mime_type": "application/json",
            "schema": _JSON_OBJECT_SCHEMA,
        }
    }
    if max_output_tokens is not None:
        kwargs["generation_config"] = {"max_output_tokens": max_output_tokens}

    interaction = client.interactions.create(
        model=gemini_provider._content_model(model),
        input=enriched + _JSON_ONLY_SUFFIX,
        **kwargs,
    )
    status = _status_value(getattr(interaction, "status", None))
    if status == "incomplete":
        raise RuntimeError("GEMINI_INTERACTION_INCOMPLETE_MAX_TOKENS")
    if status and status != "completed":
        raise RuntimeError(f"GEMINI_INTERACTION_{status.upper()}")

    raw = str(getattr(interaction, "output_text", "") or "")
    if raw_observer is not None:
        raw_observer(raw)
    if not raw.strip():
        raise RuntimeError("GEMINI_EMPTY_OUTPUT")
    return gemini_provider._parse_json_text(raw)


setattr(_guarded_gemini_json_text, _MARKER, True)


def install_gemini_planning_output_guard() -> None:
    """Use current Interactions structured JSON mode for routed planning calls only."""
    current = planner_router.gemini_json_text
    if getattr(current, _MARKER, False):
        return
    planner_router.gemini_json_text = _guarded_gemini_json_text
    print(
        "Gemini planning output guard installed: Interactions status must be completed; "
        "incomplete/empty outputs fail explicitly; native JSON object response_format enabled"
    )

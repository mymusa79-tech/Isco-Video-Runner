from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
OUTLINE_PORTABLE_MAX_PROMPT_UTF8_BYTES = 26 * 1024

_PERSONA_REQUIRED_PATHS = (
    ("writing_voice", "tone"),
    ("writing_voice", "cadence"),
    ("writing_voice", "signature_moves"),
    ("writing_voice", "banned_ai_phrases"),
    ("analysis_lens", "principle"),
    ("analysis_lens", "required_moves"),
    ("analysis_lens", "generic_rejection_rule"),
    ("dialogue_contract", "rule"),
)


def _clean_config_text(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def _validate_channel_persona_config(persona: object) -> dict[str, Any]:
    if not isinstance(persona, dict):
        raise ValueError("channel_persona_must_be_object")
    version = persona.get("version")
    if not isinstance(version, int) or version < 1:
        raise ValueError("channel_persona_version_invalid")
    if _clean_config_text(persona.get("channel")) != "نداء اليقظة":
        raise ValueError("channel_persona_channel_mismatch")
    for parent_key, child_key in _PERSONA_REQUIRED_PATHS:
        parent = persona.get(parent_key)
        value = parent.get(child_key) if isinstance(parent, dict) else None
        if isinstance(value, list):
            if not value or any(not _clean_config_text(item) for item in value):
                raise ValueError(f"channel_persona_missing_{parent_key}_{child_key}")
        elif not _clean_config_text(value):
            raise ValueError(f"channel_persona_missing_{parent_key}_{child_key}")
    return persona


def load_channel_persona() -> dict[str, Any]:
    path = ROOT / "config" / "channel_persona.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return _validate_channel_persona_config(data)


def with_channel_persona(prompt: str) -> str:
    """Port of the legacy channel-persona prompt enrichment.

    It stays provider-agnostic by enriching the prompt before ProviderRouter chooses
    Gemini, Groq, OpenRouter, or Mistral. The guard keeps repeated application safe.
    """
    if "نداء اليقظة" not in prompt or "<CHANNEL_PERSONA>" in prompt:
        return prompt
    persona_config = load_channel_persona()
    is_outline = ("EDITORIAL PREMISE CONTRACT" in prompt and "editorial_intent" in prompt) or (
        "LOCKED_EDITORIAL_PREMISE" in prompt and "section_briefs" in prompt
    )
    if is_outline:
        writing = persona_config["writing_voice"]
        dialogue = persona_config["dialogue_contract"]
        persona_config = {
            "version": persona_config["version"],
            "channel": persona_config["channel"],
            "scope": "outline_blueprint",
            "writing_voice": {
                "tone": writing["tone"],
                "signature_moves": writing["signature_moves"],
                "banned_ai_phrases": writing["banned_ai_phrases"],
            },
            "analysis_lens": persona_config["analysis_lens"],
            "dialogue_contract": {
                "rule": dialogue["rule"],
                "question_answer_rule": dialogue.get("question_answer_rule", ""),
            },
        }
    persona = json.dumps(persona_config, ensure_ascii=False, separators=(",", ":"))
    dialogue_contract = ""
    if "dialogue_qa" in prompt:
        dialogue_contract = (
            "\nDIALOGUE VOICE CONTRACT: If the selected narrative_format is dialogue_qa, every spoken turn must begin "
            "on a new line with exactly `A:` or `B:`. A is always the concise questioner/challenger. B is always the "
            "thoughtful responder and fixed primary channel voice. Use both roles in each dialogue section; never add "
            "speaker names or swap roles. The first turn should normally be B when it carries the hook/channel opener. "
            "For inner_dialogue, never use A:/B: labels: it remains one primary channel voice."
        )
    enriched = (
        prompt
        + "\n\n<CHANNEL_PERSONA>\n"
        + persona
        + "\n</CHANNEL_PERSONA>\n"
        + "CHANNEL_PERSONA is fixed editorial identity, not optional inspiration. Preserve its tone, signature moves, "
          "analysis lens, and banned-phrase rules unless a higher hard safety/factuality rule conflicts."
        + dialogue_contract
    )
    if is_outline:
        prompt_bytes = len(enriched.encode("utf-8"))
        if prompt_bytes > OUTLINE_PORTABLE_MAX_PROMPT_UTF8_BYTES:
            raise RuntimeError(
                "outline_prompt_portability_budget_exceeded "
                f"bytes={prompt_bytes} limit={OUTLINE_PORTABLE_MAX_PROMPT_UTF8_BYTES}"
            )
    return enriched


_with_channel_persona = with_channel_persona

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent


def load_human_feel() -> dict[str, list[str]]:
    """Load only story_bible.yaml:human_feel with no YAML runtime dependency."""
    path = ROOT / "config" / "story_bible.yaml"
    lines = path.read_text(encoding="utf-8").splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line == "human_feel:")
    except StopIteration:
        raise ValueError("story_bible_human_feel_missing") from None

    result: dict[str, list[str]] = {"prefer": [], "reject": []}
    active: str | None = None
    for line in lines[start + 1 :]:
        if line and not line.startswith(" "):
            break
        stripped = line.strip()
        if stripped in {"prefer:", "reject:"}:
            active = stripped[:-1]
            continue
        if not stripped:
            continue
        if active is None or not stripped.startswith("- "):
            raise ValueError("story_bible_human_feel_invalid_shape")
        raw_value = stripped[2:].strip()
        try:
            value: Any = json.loads(raw_value)
        except json.JSONDecodeError:
            raise ValueError("story_bible_human_feel_invalid_value") from None
        if not isinstance(value, str) or not value.strip():
            raise ValueError("story_bible_human_feel_invalid_value")
        result[active].append(value.strip())

    if not result["prefer"] or not result["reject"]:
        raise ValueError("story_bible_human_feel_incomplete")
    return result


def with_human_feel(prompt: str) -> str:
    """Inject the legacy story-bible human-feel rules once into channel writing prompts."""
    if "نداء اليقظة" not in prompt or "<HUMAN_FEEL>" in prompt:
        return prompt
    rules = load_human_feel()
    payload = json.dumps(rules, ensure_ascii=False, separators=(",", ":"))
    return (
        prompt
        + "\n\n<HUMAN_FEEL>\n"
        + payload
        + "\n</HUMAN_FEEL>\n"
        + "Apply HUMAN_FEEL as fixed writing guidance: prefer every listed prefer pattern where appropriate "
          "and reject every listed reject pattern. Higher hard safety and factuality rules still win."
    )

from __future__ import annotations

import re

import scripts.task_level_planner_router as router
from scripts.checkpoint_namespace_guard import install_checkpoint_namespace_guard


def _expected(prompt: str) -> int | None:
    for pattern in (
        r"Required number of sections:\s*exactly\s*(\d+)",
        r"section_briefs\s*\(exactly\s*(\d+)\)",
        r"exactly\s*(\d+)\s*section briefs",
    ):
        match = re.search(pattern, prompt, flags=re.I)
        if match:
            return int(match.group(1))
    return None


def install_schema_guard() -> None:
    # This installer runs before install_router(), so checkpoint namespace protection
    # is active before the router loads any persisted response cache.
    install_checkpoint_namespace_guard()
    original = router._normalize_outline
    if getattr(original, "_isco_planning_schema_guard", False):
        return

    def guarded(data: dict, prompt: str) -> dict:
        data = original(data, prompt)
        if "section_briefs" not in prompt.lower():
            return data

        expected = _expected(prompt)
        if expected is None:
            raise RuntimeError("OUTLINE_SCHEMA_INVALID expected section count unknown")

        briefs = data.get("section_briefs")
        if not isinstance(briefs, list):
            raise RuntimeError("OUTLINE_SCHEMA_INVALID section_briefs not list")

        valid = [
            dict(item)
            for item in briefs
            if isinstance(item, dict) and str(item.get("purpose", "")).strip()
        ]
        if len(valid) > expected:
            valid = valid[:expected]
            print(f"Outline schema guard trimmed to {expected} sections")
        if len(valid) != expected:
            raise RuntimeError(
                f"OUTLINE_SCHEMA_INVALID got={len(valid)} expected={expected}"
            )

        fixed = dict(data)
        fixed["section_briefs"] = valid
        titles = fixed.get("title_options")
        thumbs = fixed.get("thumbnail_concepts")
        if not isinstance(titles, list) or len(titles) < 3:
            raise RuntimeError("OUTLINE_SCHEMA_INVALID title_options<3")
        if not isinstance(thumbs, list) or len(thumbs) < 3:
            raise RuntimeError("OUTLINE_SCHEMA_INVALID thumbnail_concepts<3")
        fixed["title_options"] = titles[:3]
        fixed["thumbnail_concepts"] = thumbs[:3]
        return fixed

    guarded._isco_planning_schema_guard = True
    router._normalize_outline = guarded
    print("Planning schema guard installed")

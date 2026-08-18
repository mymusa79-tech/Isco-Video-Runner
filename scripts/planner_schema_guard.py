from __future__ import annotations

import re

import scripts.task_level_planner_router as router


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
    original = router._normalize_outline

    def guarded(data: dict, prompt: str) -> dict:
        print("PLANNING_BOUNDARY ENTER schema_guard")
        try:
            data = original(data, prompt)
            if "section_briefs" not in prompt.lower():
                print("PLANNING_BOUNDARY EXIT schema_guard reason=non_outline")
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
            print("PLANNING_BOUNDARY EXIT schema_guard")
            return fixed
        except Exception as exc:
            detail = str(exc).replace("\n", " ")[:220]
            print(
                "PLANNING_BOUNDARY ERROR schema_guard "
                + f"type={type(exc).__name__} detail={detail}"
            )
            raise

    router._normalize_outline = guarded
    print("Planning schema guard installed")

from __future__ import annotations

from typing import Any, Mapping


VISUAL_WORLD_DEFAULT = (
    "Grounded, hopeful cinematic realism; soft natural light; warm neutral colors; "
    "environments, hands, objects, routines and wide shots; no identifiable faces."
)
SOURCE_PREFERENCES = frozenset({"stock_motion", "ai_still"})
MAX_BEATS_PER_SECTION = 3


def fallback_visual_story(plan: Mapping[str, Any]) -> dict[str, Any]:
    sections = [item for item in (plan.get("sections") or []) if isinstance(item, Mapping)]
    if not sections:
        raise ValueError("visual story requires at least one planned section")

    purposes = [str(item.get("purpose") or "").strip() for item in sections]
    midpoint = purposes[len(purposes) // 2]
    beats = []
    for index, section in enumerate(sections, start=1):
        beats.append(
            {
                "id": f"b{index}",
                "section_id": str(section.get("id") or f"s{index}"),
                "viewer_intent": str(section.get("purpose") or "").strip(),
                "shot_intent": str(section.get("visual_query_en") or "").strip(),
                "source_preference": "stock_motion",
            }
        )
    return {
        "schema_version": 1,
        "visual_world": VISUAL_WORLD_DEFAULT,
        "story_arc": {
            "beginning": purposes[0],
            "transformation": midpoint,
            "arrival": purposes[-1],
        },
        "beats": beats,
    }


def validate_visual_story(value: Any, plan: Mapping[str, Any]) -> dict[str, Any]:
    if value is None:
        return fallback_visual_story(plan)
    if not isinstance(value, Mapping):
        raise ValueError("visual_story must be an object")

    visual_world = " ".join(str(value.get("visual_world") or "").split()).strip()
    raw_arc = value.get("story_arc")
    raw_beats = value.get("beats")
    if not visual_world or not isinstance(raw_arc, Mapping) or not isinstance(raw_beats, list):
        raise ValueError("visual_story requires visual_world, story_arc, and beats")

    arc = {
        key: " ".join(str(raw_arc.get(key) or "").split()).strip()
        for key in ("beginning", "transformation", "arrival")
    }
    if any(not text for text in arc.values()):
        raise ValueError("visual_story story_arc requires beginning, transformation, and arrival")

    sections = [item for item in (plan.get("sections") or []) if isinstance(item, Mapping)]
    section_ids = [str(item.get("id") or "").strip() for item in sections]
    section_order = {section_id: index for index, section_id in enumerate(section_ids)}
    if not section_ids:
        raise ValueError("visual_story requires planned sections")
    if not 1 <= len(raw_beats) <= max(1, len(section_ids) * MAX_BEATS_PER_SECTION):
        raise ValueError("visual_story beat count is outside the bounded per-section limit")

    beats: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    per_section = {section_id: 0 for section_id in section_ids}
    prior_section_index = -1
    for index, raw in enumerate(raw_beats, start=1):
        if not isinstance(raw, Mapping):
            raise ValueError(f"visual_story beat {index} must be an object")
        beat_id = str(raw.get("id") or f"b{index}").strip()[:40]
        section_id = str(raw.get("section_id") or "").strip()
        viewer_intent = " ".join(str(raw.get("viewer_intent") or "").split()).strip()
        shot_intent = " ".join(str(raw.get("shot_intent") or "").split()).strip()
        source_preference = str(raw.get("source_preference") or "").strip()

        if not beat_id or beat_id in seen_ids:
            raise ValueError("visual_story beat ids must be unique and non-empty")
        if section_id not in section_order:
            raise ValueError(f"visual_story beat {beat_id} references an unknown section")
        if section_order[section_id] < prior_section_index:
            raise ValueError("visual_story beats must follow planned section order")
        if not viewer_intent or not shot_intent:
            raise ValueError(f"visual_story beat {beat_id} requires viewer_intent and shot_intent")
        if len(viewer_intent) > 600 or len(shot_intent) > 260:
            raise ValueError(f"visual_story beat {beat_id} is too verbose")
        if source_preference not in SOURCE_PREFERENCES:
            raise ValueError(
                f"visual_story beat {beat_id} source_preference must be stock_motion or ai_still"
            )

        prior_section_index = section_order[section_id]
        per_section[section_id] += 1
        if per_section[section_id] > MAX_BEATS_PER_SECTION:
            raise ValueError(
                f"visual_story section {section_id} exceeds {MAX_BEATS_PER_SECTION} beats"
            )
        seen_ids.add(beat_id)
        beats.append(
            {
                "id": beat_id,
                "section_id": section_id,
                "viewer_intent": viewer_intent,
                "shot_intent": shot_intent,
                "source_preference": source_preference,
            }
        )

    missing = [section_id for section_id, count in per_section.items() if count == 0]
    if missing:
        raise ValueError(
            "visual_story must cover every planned section: missing=" + ",".join(missing)
        )

    return {
        "schema_version": 1,
        "visual_world": visual_world[:800],
        "story_arc": {key: text[:400] for key, text in arc.items()},
        "beats": beats,
    }


def _context_fragment(value: object, fallback: str, limit: int) -> str:
    text = " ".join(str(value or "").split()).strip() or fallback
    if len(text) <= limit:
        return text
    clipped = text[:limit].rsplit(" ", 1)[0].strip()
    return clipped or text[:limit].strip()


def contextual_intent(
    visual_story: Mapping[str, Any],
    beat_id: str,
    fallback_intent: str,
) -> str:
    beats = [item for item in (visual_story.get("beats") or []) if isinstance(item, Mapping)]
    current_index = next(
        (index for index, item in enumerate(beats) if str(item.get("id") or "") == beat_id),
        None,
    )
    if current_index is None:
        return str(fallback_intent or "").strip()[:300]

    # Canonical visual evidence intentionally caps intended_visual at 300 chars.
    # Bound each neighbor independently so long shot intents can never crowd the
    # following beat out of the story-context judgment.
    current = _context_fragment(
        beats[current_index].get("shot_intent") or fallback_intent,
        "current beat",
        60,
    )
    previous = _context_fragment(
        beats[current_index - 1].get("shot_intent") if current_index > 0 else "",
        "story opening",
        48,
    )
    following = _context_fragment(
        beats[current_index + 1].get("shot_intent")
        if current_index + 1 < len(beats)
        else "",
        "story arrival",
        48,
    )
    return (
        f"Current: {current}. Previous: {previous}. Next: {following}. "
        "Same story arc: judge whether this shot belongs naturally between its neighbors."
    )

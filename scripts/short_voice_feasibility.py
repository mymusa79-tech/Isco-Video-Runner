from __future__ import annotations

from itertools import combinations
from typing import Any


SCHEMA_VERSION = 1
NATURAL_WORDS_PER_SECOND = 1.75
INTER_BEAT_PAUSE_SECONDS = 0.22
PREFLIGHT_SPEED_HEADROOM = 1.10
RUNTIME_MAX_SPEED = 1.20
TAIL_GUARD_SECONDS = 0.15


def _clean(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def _words(text: str) -> int:
    return len([token for token in _clean(text).split(" ") if token])


def _estimate_seconds(texts: list[str]) -> float:
    word_seconds = sum(_words(text) for text in texts) / NATURAL_WORDS_PER_SECOND
    pause_seconds = max(0, len(texts) - 1) * INTER_BEAT_PAUSE_SECONDS
    return round(word_seconds + pause_seconds, 3)


def _transcript(texts: list[str]) -> str:
    return "، ".join(text.rstrip(" .،!?؟") for text in texts) + "."


def _candidate_index_sets(count: int, mode: str) -> list[list[int]]:
    if count < 2:
        return []
    if mode == "hybrid":
        return [[0, count - 1]]
    if mode != "voice_led":
        raise RuntimeError("Short Voice feasibility received unsupported voice mode")

    # Hook and payoff are mandatory. Search every middle-beat subset by descending
    # cardinality so the first feasible projection always preserves the maximum
    # number of already-approved semantic beats. For equal cardinality prefer the
    # latest semantic turns first, preserving the prior policy without allowing an
    # untested earlier subset to be skipped entirely.
    middle = tuple(range(1, count - 1))
    candidates: list[list[int]] = []
    for keep_count in range(len(middle), -1, -1):
        variants = sorted(combinations(middle, keep_count), reverse=True)
        for variant in variants:
            candidates.append([0, *variant, count - 1])
    return candidates


def build_voice_projection(
    events: list[dict[str, Any]],
    mode: str,
    *,
    final_seconds: float,
    exclude_index_sets: list[list[int]] | None = None,
) -> dict[str, Any]:
    """Choose the richest semantic narration that can fit naturally before TTS.

    On-screen beats remain unchanged. This function only decides which already-approved
    semantic beats are spoken. It performs no AI rewrite and never relaxes the runtime
    1.20x hard speed ceiling.

    ``exclude_index_sets`` lets a caller ask for the next-richest candidate after one
    or more already-tried projections were found too dense at the REAL (post-synthesis)
    measurement rather than this function's word-rate estimate - Run #196 confirmed a
    real Gemini narration can come back denser than NATURAL_WORDS_PER_SECOND predicts.
    Candidates remain tried in the same max-beats-first order; this only skips entries
    a caller has already spent a real TTS call on.
    """
    texts = [
        _clean(item.get("text"))
        for item in events
        if isinstance(item, dict) and _clean(item.get("text"))
    ]
    if len(texts) < 2:
        raise RuntimeError("Short Voice feasibility requires at least two semantic beats")
    if final_seconds <= TAIL_GUARD_SECONDS:
        raise RuntimeError("Short Voice feasibility received invalid final duration")

    natural_window = final_seconds - TAIL_GUARD_SECONDS
    planning_budget = natural_window * PREFLIGHT_SPEED_HEADROOM
    excluded = {tuple(indexes) for indexes in (exclude_index_sets or [])}

    chosen_indexes: list[int] | None = None
    chosen_estimate = 0.0
    candidates = _candidate_index_sets(len(texts), mode)
    for indexes in candidates:
        if tuple(indexes) in excluded:
            continue
        candidate = [texts[index] for index in indexes]
        estimate = _estimate_seconds(candidate)
        if estimate <= planning_budget:
            chosen_indexes = indexes
            chosen_estimate = estimate
            break

    if chosen_indexes is None:
        minimum = [texts[0], texts[-1]]
        minimum_estimate = _estimate_seconds(minimum)
        raise RuntimeError(
            "Short Voice V2 semantic voice budget is impossible without rewriting: "
            f"minimum_estimated_seconds={minimum_estimate:.3f} "
            f"planning_budget_seconds={planning_budget:.3f}"
        )

    chosen = [texts[index] for index in chosen_indexes]
    omitted = [index for index in range(len(texts)) if index not in set(chosen_indexes)]
    strategy = "full_semantic_progression" if not omitted else "bounded_semantic_projection"
    return {
        "schema_version": SCHEMA_VERSION,
        "strategy": strategy,
        "mode": mode,
        "transcript": _transcript(chosen),
        "original_beat_count": len(texts),
        "spoken_beat_count": len(chosen),
        "spoken_beat_indexes": chosen_indexes,
        "omitted_beat_indexes": omitted,
        "estimated_natural_seconds": chosen_estimate,
        "planning_budget_seconds": round(planning_budget, 3),
        "final_seconds": round(final_seconds, 3),
        "natural_words_per_second": NATURAL_WORDS_PER_SECOND,
        "inter_beat_pause_seconds": INTER_BEAT_PAUSE_SECONDS,
        "preflight_speed_headroom": PREFLIGHT_SPEED_HEADROOM,
        "runtime_max_speed_unchanged": RUNTIME_MAX_SPEED,
        "projection_search_policy": "max_spoken_beat_count_then_latest_semantic_turn",
        "projection_candidates_considered": len(candidates),
        "projection_search_complete": True,
        "ai_rewrite_used": False,
        "on_screen_semantic_beats_preserved": True,
    }
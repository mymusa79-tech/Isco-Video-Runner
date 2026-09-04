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


def _projection_payload(
    texts: list[str],
    indexes: list[int],
    *,
    mode: str,
    final_seconds: float,
    planning_budget: float,
    strategy: str,
    measured_speed_required: float | None = None,
) -> dict[str, Any]:
    chosen = [texts[index] for index in indexes]
    omitted = [index for index in range(len(texts)) if index not in set(indexes)]
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "strategy": strategy,
        "mode": mode,
        "transcript": _transcript(chosen),
        "original_beat_count": len(texts),
        "spoken_beat_count": len(chosen),
        "spoken_beat_indexes": indexes,
        "omitted_beat_indexes": omitted,
        "estimated_natural_seconds": _estimate_seconds(chosen),
        "planning_budget_seconds": round(planning_budget, 3),
        "final_seconds": round(final_seconds, 3),
        "natural_words_per_second": NATURAL_WORDS_PER_SECOND,
        "inter_beat_pause_seconds": INTER_BEAT_PAUSE_SECONDS,
        "preflight_speed_headroom": PREFLIGHT_SPEED_HEADROOM,
        "runtime_max_speed_unchanged": RUNTIME_MAX_SPEED,
        "projection_search_policy": "max_spoken_beat_count_then_latest_semantic_turn",
        "ai_rewrite_used": False,
        "on_screen_semantic_beats_preserved": True,
    }
    if measured_speed_required is not None:
        result.update(
            {
                "runtime_measured_repair": True,
                "runtime_measured_speed_required": round(float(measured_speed_required), 3),
                "runtime_repair_policy": "strictly_reduce_spoken_semantic_beats_once",
            }
        )
    return result


def build_voice_projection(
    events: list[dict[str, Any]],
    mode: str,
    *,
    final_seconds: float,
) -> dict[str, Any]:
    """Choose the richest semantic narration that can fit naturally before TTS.

    On-screen beats remain unchanged. This function only decides which already-approved
    semantic beats are spoken. It performs no AI rewrite and never relaxes the runtime
    1.20x hard speed ceiling.
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

    chosen_indexes: list[int] | None = None
    candidates = _candidate_index_sets(len(texts), mode)
    for indexes in candidates:
        candidate = [texts[index] for index in indexes]
        estimate = _estimate_seconds(candidate)
        if estimate <= planning_budget:
            chosen_indexes = indexes
            break

    if chosen_indexes is None:
        minimum = [texts[0], texts[-1]]
        minimum_estimate = _estimate_seconds(minimum)
        raise RuntimeError(
            "Short Voice V2 semantic voice budget is impossible without rewriting: "
            f"minimum_estimated_seconds={minimum_estimate:.3f} "
            f"planning_budget_seconds={planning_budget:.3f}"
        )

    omitted = [index for index in range(len(texts)) if index not in set(chosen_indexes)]
    strategy = "full_semantic_progression" if not omitted else "bounded_semantic_projection"
    result = _projection_payload(
        texts,
        chosen_indexes,
        mode=mode,
        final_seconds=final_seconds,
        planning_budget=planning_budget,
        strategy=strategy,
    )
    result["projection_candidates_considered"] = len(candidates)
    result["projection_search_complete"] = True
    return result


def build_runtime_repair_projection(
    events: list[dict[str, Any]],
    mode: str,
    *,
    final_seconds: float,
    current_projection: dict[str, Any],
    measured_speed_required: float,
) -> dict[str, Any]:
    """Choose one strictly smaller spoken projection after real TTS proves preflight wrong.

    This is a measured-duration recovery, not another heuristic retry. It is deliberately
    bounded to one semantic contraction: all on-screen beats remain intact, hook/payoff
    remain mandatory, no text model is called, and the 1.20x runtime ceiling is unchanged.
    """
    if float(measured_speed_required) <= RUNTIME_MAX_SPEED:
        raise RuntimeError("Short Voice measured repair requires a real runtime overflow")
    texts = [
        _clean(item.get("text"))
        for item in events
        if isinstance(item, dict) and _clean(item.get("text"))
    ]
    if len(texts) < 2:
        raise RuntimeError("Short Voice measured repair requires at least two semantic beats")
    current = tuple(int(index) for index in list(current_projection.get("spoken_beat_indexes") or []))
    if len(current) < 2:
        raise RuntimeError("Short Voice measured repair received invalid current projection")

    natural_window = final_seconds - TAIL_GUARD_SECONDS
    planning_budget = natural_window * PREFLIGHT_SPEED_HEADROOM
    candidates = _candidate_index_sets(len(texts), mode)
    smaller = [
        indexes
        for indexes in candidates
        if len(indexes) < len(current)
        and indexes[0] == 0
        and indexes[-1] == len(texts) - 1
    ]
    if not smaller:
        raise RuntimeError(
            "Short Voice measured narration remains too dense at the minimum semantic projection; "
            "planning rewrite is required"
        )

    chosen = smaller[0]
    result = _projection_payload(
        texts,
        chosen,
        mode=mode,
        final_seconds=final_seconds,
        planning_budget=planning_budget,
        strategy="measured_duration_repair_projection",
        measured_speed_required=measured_speed_required,
    )
    result["projection_candidates_considered"] = len(candidates)
    result["projection_search_complete"] = True
    result["runtime_repair_from_spoken_beat_indexes"] = list(current)
    return result

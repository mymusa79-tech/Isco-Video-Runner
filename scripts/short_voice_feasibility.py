from __future__ import annotations

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

    candidates: list[list[int]] = [list(range(count))]
    middle = list(range(1, count - 1))
    if middle:
        # Preserve the hook and payoff first, then keep the latest semantic turn.
        # Additional middle beats are admitted only while the natural-duration budget allows.
        candidates.append([0, middle[-1], count - 1])
    candidates.append([0, count - 1])

    unique: list[list[int]] = []
    seen: set[tuple[int, ...]] = set()
    for item in candidates:
        key = tuple(item)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


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
    chosen_estimate = 0.0
    for indexes in _candidate_index_sets(len(texts), mode):
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
        "ai_rewrite_used": False,
        "on_screen_semantic_beats_preserved": True,
    }

from __future__ import annotations

"""One production-feasibility authority for long-form planning and rendered duration.

Run187 exposed a family-level contract drift: planning, narration word budgets and
final media QC could each accept a different duration envelope.  This module makes the
long-form feasibility decision explicit and versioned, then installs two small runtime
bindings against the pinned private Engine:

1. Film/Story planning uses a word envelope that is representable inside the final
   media-duration contract for a conservative spoken-rate envelope.
2. The actual concatenated narration is measured before any stock-media search,
   download, visual review or FFmpeg picture render begins.  The final duration gate
   reuses the same duration authority as defense in depth.

Standalone Moment remains owned by native_short_stage_contract; this module reads that
existing contract for family visibility instead of copying its 12-20s constants.
The contract does not lower any existing quality gate and does not add provider calls.
"""

from dataclasses import dataclass
import json
import math
from pathlib import Path

import isco_video_agent.orchestrator as orchestrator
import isco_video_agent.resilient_planner as resilient_planner
from isco_video_agent.config import load_channel_config

from scripts import native_short_stage_contract as moment_contract


CONTRACT_ID = "production-feasibility-v1"
CONTRACT_VERSION = 1

# Conservative spoken-rate envelope used only to prove that a narration word range can
# map into the final media window.  Actual acceptance is always based on probed seconds.
SPEECH_WPM_FLOOR = 105.0
SPEECH_WPM_CEILING = 150.0

# Preserve the mature Film band. Story's historical 260-800 range is intentionally the
# input to the intersection calculation rather than a second authority.
_BASE_LONG_WORD_BOUNDS: dict[str, tuple[int, int]] = {
    "film": (800, 1450),
    "story": (260, 800),
}

# Film is already mature and safe, so preserve its authored 960-word target exactly.
# Story intentionally uses the midpoint of its newly feasible envelope to stop living
# on the old 420-word lower edge.
_PRESERVED_TARGET_WORDS: dict[str, int] = {
    "film": 960,
}

# These ratios are the current Engine final-QC policy. Keeping them here makes planning,
# pre-visual audio feasibility and final QC consume one source instead of three copies.
_FINAL_DURATION_RATIOS: dict[str, tuple[float, float]] = {
    "film": (0.625, 1.875),
    "story": (0.70, 1.50),
}


class ProductionFeasibilityError(RuntimeError):
    """A plan/audio artifact cannot satisfy the production duration contract."""


@dataclass(frozen=True, slots=True)
class FeasibilitySpec:
    format: str
    target_seconds: float
    min_seconds: float
    max_seconds: float
    min_words: int | None
    target_words: int | None
    max_words: int | None


def _config(cfg: dict | None = None) -> dict:
    return cfg if cfg is not None else load_channel_config()


def _moment_duration_bounds() -> tuple[float, float]:
    """Read the already-authoritative Run187 Moment contract without duplicating it."""
    stage = moment_contract.moment_stage_spec("short_draft", "feasibility-contract-probe")
    return (
        float(stage.semantic_rules["expected_seconds_min"]),
        float(stage.semantic_rules["expected_seconds_max"]),
    )


def final_duration_bounds(fmt: str, cfg: dict | None = None) -> tuple[float, float]:
    fmt = str(fmt or "").strip().lower()
    if fmt == "moment":
        return _moment_duration_bounds()
    if fmt not in _FINAL_DURATION_RATIOS:
        raise ProductionFeasibilityError(f"unsupported_feasibility_format:{fmt or 'missing'}")
    config = _config(cfg)
    target = float(config["formats"][fmt]["target_seconds"])
    low_ratio, high_ratio = _FINAL_DURATION_RATIOS[fmt]
    return target * low_ratio, target * high_ratio


def long_word_bounds(fmt: str, cfg: dict | None = None) -> tuple[int, int]:
    """Return the old editorial range intersected with renderable speech time."""
    fmt = str(fmt or "").strip().lower()
    if fmt not in _BASE_LONG_WORD_BOUNDS:
        raise ProductionFeasibilityError(f"unsupported_long_format:{fmt or 'missing'}")
    min_seconds, max_seconds = final_duration_bounds(fmt, cfg)
    base_min, base_max = _BASE_LONG_WORD_BOUNDS[fmt]
    time_safe_min = int(math.ceil(min_seconds * SPEECH_WPM_CEILING / 60.0))
    time_safe_max = int(math.floor(max_seconds * SPEECH_WPM_FLOOR / 60.0))
    lower = max(base_min, time_safe_min)
    upper = min(base_max, time_safe_max)
    if lower > upper:
        raise ProductionFeasibilityError(
            f"empty_word_duration_intersection:{fmt}:{lower}>{upper}"
        )
    return lower, upper


def target_words(fmt: str, cfg: dict | None = None) -> int:
    normalized = str(fmt or "").strip().lower()
    if normalized in _PRESERVED_TARGET_WORDS:
        # Still validate that the preserved target belongs to the current feasible band.
        lower, upper = long_word_bounds(normalized, cfg)
        preserved = _PRESERVED_TARGET_WORDS[normalized]
        if not lower <= preserved <= upper:
            raise ProductionFeasibilityError(
                f"preserved_target_outside_feasible_band:{normalized}:{preserved}"
            )
        return preserved
    lower, upper = long_word_bounds(normalized, cfg)
    return int(round((lower + upper) / 2.0))


def spec(fmt: str, cfg: dict | None = None) -> FeasibilitySpec:
    fmt = str(fmt or "").strip().lower()
    config = _config(cfg)
    if fmt == "moment":
        low, high = final_duration_bounds(fmt, config)
        return FeasibilitySpec(fmt, float(config["formats"][fmt]["target_seconds"]), low, high, None, None, None)
    low, high = final_duration_bounds(fmt, config)
    word_low, word_high = long_word_bounds(fmt, config)
    return FeasibilitySpec(
        fmt,
        float(config["formats"][fmt]["target_seconds"]),
        low,
        high,
        word_low,
        target_words(fmt, config),
        word_high,
    )


def _write_evidence(output_dir: Path, *, fmt: str, seconds: float, accepted: bool) -> None:
    current = spec(fmt)
    payload = {
        "contract_id": CONTRACT_ID,
        "contract_version": CONTRACT_VERSION,
        "format": fmt,
        "stage": "post_tts_pre_visual",
        "actual_narration_seconds": round(float(seconds), 3),
        "min_seconds": current.min_seconds,
        "max_seconds": current.max_seconds,
        "min_words": current.min_words,
        "target_words": current.target_words,
        "max_words": current.max_words,
        "accepted": bool(accepted),
    }
    (Path(output_dir) / "production-feasibility.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def validate_actual_narration(output_dir: Path, narration: Path, *, fmt: str) -> float:
    """Probe real narration seconds and fail before visual acquisition when infeasible."""
    fmt = str(fmt or "").strip().lower()
    if fmt not in _FINAL_DURATION_RATIOS:
        return 0.0
    seconds = float(orchestrator.duration(Path(narration)))
    low, high = final_duration_bounds(fmt)
    accepted = low <= seconds <= high
    _write_evidence(Path(output_dir), fmt=fmt, seconds=seconds, accepted=accepted)
    if not accepted:
        raise ProductionFeasibilityError(
            "PRODUCTION_FEASIBILITY_AUDIO_OUTSIDE_CONTRACT "
            f"format={fmt} actual={seconds:.3f}s required={low:.3f}-{high:.3f}s "
            "stage=post_tts_pre_visual"
        )
    return seconds


def _install_planning_binding() -> None:
    for fmt in ("film", "story"):
        resilient_planner._DURATION_WORD_BOUNDS[fmt] = long_word_bounds(fmt)
        resilient_planner._TARGET_TOTAL_WORDS[fmt] = target_words(fmt)


def _install_final_duration_binding() -> None:
    current = orchestrator._duration_limits
    if getattr(current, "_isco_production_feasibility_contract", False) is True:
        return

    def wrapped(cfg: dict, fmt: str) -> tuple[float, float]:
        normalized = str(fmt or "").strip().lower()
        if normalized in _FINAL_DURATION_RATIOS:
            return final_duration_bounds(normalized, cfg)
        return current(cfg, fmt)

    wrapped._isco_production_feasibility_contract = True
    wrapped._isco_production_feasibility_original = current
    orchestrator._duration_limits = wrapped


def _install_post_tts_pre_visual_binding() -> None:
    current = orchestrator.concat_audio
    if getattr(current, "_isco_production_feasibility_contract", False) is True:
        return

    def wrapped(inputs: list[Path], output: Path) -> Path:
        result = current(inputs, output)
        out_path = Path(result)
        # The production narration path is written only after plan.json already exists.
        # Other concat_audio callers retain historical behavior unchanged.
        if out_path.name != "narration.wav":
            return result
        plan_path = out_path.parent / "plan.json"
        if not plan_path.is_file():
            raise ProductionFeasibilityError(
                "PRODUCTION_FEASIBILITY_PLAN_MISSING stage=post_tts_pre_visual"
            )
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            raise ProductionFeasibilityError(
                "PRODUCTION_FEASIBILITY_PLAN_INVALID stage=post_tts_pre_visual"
            ) from exc
        fmt = str(plan.get("format") or "").strip().lower() if isinstance(plan, dict) else ""
        if fmt in _FINAL_DURATION_RATIOS:
            validate_actual_narration(out_path.parent, out_path, fmt=fmt)
        return result

    wrapped._isco_production_feasibility_contract = True
    wrapped._isco_production_feasibility_original = current
    orchestrator.concat_audio = wrapped


def install_production_feasibility_contract() -> dict[str, object]:
    """Install the family closure exactly once without provider calls or gate lowering."""
    _install_planning_binding()
    _install_final_duration_binding()
    _install_post_tts_pre_visual_binding()
    film = spec("film")
    story = spec("story")
    return {
        "contract_id": CONTRACT_ID,
        "contract_version": CONTRACT_VERSION,
        "film_word_bounds": (film.min_words, film.max_words),
        "film_target_words": film.target_words,
        "film_duration_bounds": (film.min_seconds, film.max_seconds),
        "story_word_bounds": (story.min_words, story.max_words),
        "story_target_words": story.target_words,
        "story_duration_bounds": (story.min_seconds, story.max_seconds),
        "moment_duration_bounds": _moment_duration_bounds(),
        "post_tts_pre_visual_gate": True,
    }

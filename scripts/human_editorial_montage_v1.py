from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from isco_video_agent.media.ffmpeg import duration
from isco_video_agent.security import secret_free_subprocess_env


PROFILE = "human_editorial_montage_v1"
MAX_CUT_OFFSET_SECONDS = 0.22
MIN_BEAT_SECONDS = 0.70
FRAME_GRID = 12

# Keep the hook cut early for every template so the Run219 hook-window cap can never
# regress. Later boundaries vary by editorial role instead of landing mechanically on
# every text boundary. Values stay inside the 80-220ms human micro-timing band.
_BOUNDARY_OFFSETS: dict[str, tuple[float, ...]] = {
    "why_reframe": (-0.16, 0.08, -0.11),
    "inner_dialogue": (-0.11, 0.14, -0.08),
    "micro_story": (-0.08, -0.12, 0.09),
    "quote_reflection": (-0.06, 0.10, 0.14),
}

_TEXT_REVEAL_DELAY: dict[str, float] = {
    "why_reframe": 0.14,
    "inner_dialogue": 0.12,
    "micro_story": 0.10,
    "quote_reflection": 0.16,
}

_SOUND_BRIDGE_PRELAP: dict[str, float] = {
    "why_reframe": 0.18,
    "inner_dialogue": 0.16,
    "micro_story": 0.14,
    "quote_reflection": 0.18,
}

_COMPOSITION_LOCK = threading.RLock()


class HumanEditorialMontageError(RuntimeError):
    pass


def _clean(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def _seconds(value: object) -> float:
    if isinstance(value, bool):
        raise HumanEditorialMontageError("invalid_timing")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise HumanEditorialMontageError("invalid_timing") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise HumanEditorialMontageError("invalid_timing")
    return parsed


def _template_offsets(template: object) -> tuple[float, ...]:
    key = _clean(template)
    try:
        return _BOUNDARY_OFFSETS[key]
    except KeyError as exc:
        raise HumanEditorialMontageError(f"unsupported_short_template:{key or 'empty'}") from exc


def humanize_event_windows(
    events: Sequence[dict[str, Any]],
    template: object,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Move only internal visual/text boundaries by restrained editor-like microtiming.

    Text, role order, first start and final end remain exact. The function never changes
    narration speed and never creates an overlap; it only makes the cut rhythm less
    mechanically identical to authored text boundaries.
    """
    if len(events) < 2:
        raise HumanEditorialMontageError("human_montage_requires_two_beats")
    offsets = _template_offsets(template)
    original = [dict(item) for item in events]
    result = [dict(item) for item in events]
    for item in result:
        _seconds(item.get("start"))
        _seconds(item.get("end"))

    applied: list[dict[str, Any]] = []
    for index in range(len(result) - 1):
        left = result[index]
        right = result[index + 1]
        left_start = _seconds(left.get("start"))
        left_end = _seconds(left.get("end"))
        right_start = _seconds(right.get("start"))
        right_end = _seconds(right.get("end"))
        if left_end <= left_start or right_end <= right_start:
            raise HumanEditorialMontageError("human_montage_non_positive_beat")

        # Voice-owned events are normally contiguous. If a tiny authored gap exists,
        # place the editorial cut at its midpoint before applying the bounded offset.
        boundary = (left_end + right_start) / 2.0
        raw_offset = offsets[min(index, len(offsets) - 1)]
        raw_offset = max(-MAX_CUT_OFFSET_SECONDS, min(MAX_CUT_OFFSET_SECONDS, raw_offset))
        minimum = left_start + MIN_BEAT_SECONDS
        maximum = right_end - MIN_BEAT_SECONDS
        if maximum <= minimum:
            shifted = boundary
            effective = 0.0
        else:
            shifted = max(minimum, min(maximum, boundary + raw_offset))
            effective = shifted - boundary

        left["end"] = round(shifted, 3)
        right["start"] = round(shifted, 3)
        applied.append(
            {
                "boundary_after_index": index,
                "requested_offset_ms": int(round(raw_offset * 1000)),
                "effective_offset_ms": int(round(effective * 1000)),
            }
        )

    # The contract is semantic-preserving: copy and roles cannot drift.
    for before, after in zip(original, result):
        if _clean(before.get("text")) != _clean(after.get("text")):
            raise HumanEditorialMontageError("human_montage_text_mutation")
        if _clean(before.get("role")) != _clean(after.get("role")):
            raise HumanEditorialMontageError("human_montage_role_mutation")
    if abs(_seconds(original[0].get("start")) - _seconds(result[0].get("start"))) > 0.001:
        raise HumanEditorialMontageError("human_montage_start_drift")
    if abs(_seconds(original[-1].get("end")) - _seconds(result[-1].get("end"))) > 0.001:
        raise HumanEditorialMontageError("human_montage_end_drift")

    evidence = {
        "profile": PROFILE,
        "status": "applied",
        "strategy": "role_template_microtiming_hard_cuts",
        "transition_pack_added": False,
        "max_cut_offset_ms": int(MAX_CUT_OFFSET_SECONDS * 1000),
        "boundaries": applied,
        "words_changed": False,
        "speech_speed_changed": False,
        "total_timeline_preserved": True,
        "extra_ai_calls": 0,
    }
    return result, evidence


def choreograph_text_events(
    events: Sequence[dict[str, Any]],
    template: object,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Delay exactly one non-hook reveal while reusing the Engine rich-text renderer."""
    key = _clean(template)
    delay = _TEXT_REVEAL_DELAY.get(key)
    if delay is None:
        raise HumanEditorialMontageError(f"unsupported_short_template:{key or 'empty'}")
    copied = [dict(item) for item in events]
    for item in copied:
        item["template"] = key
    if len(copied) < 2:
        return copied, {
            "profile": PROFILE,
            "status": "not_applicable",
            "reason": "too_few_events",
            "extra_ai_calls": 0,
        }

    # Prefer the final middle/turn beat. With a two-beat Short, delay only the payoff;
    # the hook is never delayed because first-frame commitment remains authoritative.
    index = len(copied) - 2 if len(copied) >= 3 else len(copied) - 1
    index = max(1, index)
    selected = copied[index]
    start = _seconds(selected.get("start"))
    end = _seconds(selected.get("end"))
    effective = min(delay, max(0.0, end - start - 0.80))
    if effective > 0.0:
        selected["start"] = round(start + effective, 3)

    evidence = {
        "profile": PROFILE,
        "status": "applied" if effective > 0.0 else "not_applicable_short_window",
        "event_index": index,
        "role": _clean(selected.get("role")),
        "delay_ms": int(round(effective * 1000)),
        "strategy": "single_selective_delayed_reveal_plus_existing_rich_focus",
        "hook_delayed": False,
        "words_changed": False,
        "word_level_alignment_claimed": False,
        "extra_ai_calls": 0,
    }
    return copied, evidence


def sound_bridge_events(
    events: Sequence[dict[str, Any]],
    template: object,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Create a temporary SFX-timing view with one pre-lapped payoff boundary."""
    key = _clean(template)
    prelap = _SOUND_BRIDGE_PRELAP.get(key)
    if prelap is None:
        raise HumanEditorialMontageError(f"unsupported_short_template:{key or 'empty'}")
    copied = [dict(item) for item in events]
    if len(copied) < 2:
        return copied, {"status": "not_applicable", "extra_ai_calls": 0}
    payoff = copied[-1]
    original = _seconds(payoff.get("start"))
    shifted = max(0.0, original - prelap)
    payoff["start"] = round(shifted, 3)
    return copied, {
        "profile": PROFILE,
        "status": "prepared",
        "strategy": "existing_single_sfx_payoff_prelap",
        "payoff_boundary_seconds": round(original, 3),
        "sfx_time_seconds": round(shifted, 3),
        "prelap_ms": int(round((original - shifted) * 1000)),
        "extra_sfx_events": 0,
        "extra_ai_calls": 0,
    }


def _frame_rgb(video: Path, at_seconds: float) -> bytes:
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{max(0.0, at_seconds):.3f}",
        "-i", str(video), "-frames:v", "1", "-vf",
        f"scale={FRAME_GRID}:{FRAME_GRID}:flags=bilinear,format=rgb24",
        "-f", "rawvideo", "pipe:1",
    ]
    completed = subprocess.run(
        command,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=secret_free_subprocess_env(),
    )
    expected = FRAME_GRID * FRAME_GRID * 3
    if len(completed.stdout) != expected:
        raise HumanEditorialMontageError("continuity_frame_probe_invalid")
    return completed.stdout


def _frame_features(raw: bytes) -> dict[str, float]:
    if len(raw) != FRAME_GRID * FRAME_GRID * 3:
        raise HumanEditorialMontageError("continuity_frame_shape_invalid")
    pixels: list[tuple[int, int, int]] = [
        (raw[i], raw[i + 1], raw[i + 2]) for i in range(0, len(raw), 3)
    ]
    luma = [(0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0 for r, g, b in pixels]
    mean_luma = sum(luma) / len(luma)
    mean_r = sum(p[0] for p in pixels) / len(pixels) / 255.0
    mean_b = sum(p[2] for p in pixels) / len(pixels) / 255.0
    warmth = mean_r - mean_b

    edge_total = 0.0
    edge_count = 0
    for y in range(FRAME_GRID):
        for x in range(FRAME_GRID):
            here = luma[y * FRAME_GRID + x]
            if x + 1 < FRAME_GRID:
                edge_total += abs(here - luma[y * FRAME_GRID + x + 1])
                edge_count += 1
            if y + 1 < FRAME_GRID:
                edge_total += abs(here - luma[(y + 1) * FRAME_GRID + x])
                edge_count += 1
    edge_density = edge_total / max(1, edge_count)

    weight = sum(luma) + 1e-6
    center_x = sum((i % FRAME_GRID) / (FRAME_GRID - 1) * value for i, value in enumerate(luma)) / weight
    center_y = sum((i // FRAME_GRID) / (FRAME_GRID - 1) * value for i, value in enumerate(luma)) / weight
    return {
        "luma": mean_luma,
        "warmth": warmth,
        "edge_density": edge_density,
        "center_x": center_x,
        "center_y": center_y,
    }


def _motion_energy(first: bytes, second: bytes) -> float:
    if len(first) != len(second) or not first:
        raise HumanEditorialMontageError("continuity_motion_probe_invalid")
    return sum(abs(a - b) for a, b in zip(first, second)) / len(first) / 255.0


def continuity_score(previous: dict[str, float], candidate: dict[str, float]) -> float:
    """Local adjacency score: luminance, warmth, edge/composition and motion speed."""
    center_distance = math.hypot(
        previous.get("center_x", 0.5) - candidate.get("center_x", 0.5),
        previous.get("center_y", 0.5) - candidate.get("center_y", 0.5),
    ) / math.sqrt(2.0)
    penalty = (
        0.22 * abs(previous.get("luma", 0.5) - candidate.get("luma", 0.5))
        + 0.14 * min(1.0, abs(previous.get("warmth", 0.0) - candidate.get("warmth", 0.0)) / 2.0)
        + 0.18 * abs(previous.get("edge_density", 0.0) - candidate.get("edge_density", 0.0))
        + 0.20 * center_distance
        + 0.26 * abs(previous.get("motion", 0.0) - candidate.get("motion", 0.0))
    )
    return round(max(0.0, min(1.0, 1.0 - penalty)), 6)


def _boundary_signature(video: Path, *, start_seconds: float, outgoing: bool) -> dict[str, float]:
    total = float(duration(video))
    if total <= 0.1:
        raise HumanEditorialMontageError("continuity_video_duration_invalid")
    if outgoing:
        second_time = max(0.0, total - 0.04)
        first_time = max(0.0, second_time - 0.20)
        first = _frame_rgb(video, first_time)
        second = _frame_rgb(video, second_time)
        features = _frame_features(second)
    else:
        first_time = max(0.0, start_seconds + 0.04)
        second_time = min(max(first_time, total - 0.04), first_time + 0.20)
        if second_time <= first_time + 0.01:
            second_time = min(total - 0.01, first_time + 0.05)
        first = _frame_rgb(video, first_time)
        second = _frame_rgb(video, second_time)
        features = _frame_features(first)
    features["motion"] = _motion_energy(first, second)
    return features


def choose_match_offset(previous_clip: Path, candidate_raw: Path, target_seconds: float) -> dict[str, Any]:
    """Choose among three local start windows; no new asset search or Vision call."""
    try:
        raw_seconds = float(duration(candidate_raw))
        target = max(0.1, float(target_seconds))
        slack = max(0.0, raw_seconds - target)
        if slack < 0.24:
            return {
                "status": "no_source_slack",
                "start_offset_seconds": 0.0,
                "candidate_windows": 1,
                "extra_ai_calls": 0,
            }
        previous = _boundary_signature(previous_clip, start_seconds=0.0, outgoing=True)
        offsets = sorted({0.0, round(slack * 0.5, 3), round(slack, 3)})
        scored: list[dict[str, Any]] = []
        for offset in offsets:
            signature = _boundary_signature(candidate_raw, start_seconds=offset, outgoing=False)
            score = continuity_score(previous, signature)
            scored.append({"offset": offset, "score": score, "signature": signature})
        best = max(scored, key=lambda row: (float(row["score"]), -float(row["offset"])))
        return {
            "status": "selected",
            "start_offset_seconds": float(best["offset"]),
            "match_score": float(best["score"]),
            "candidate_windows": len(scored),
            "criteria": ["luminance", "warmth", "edge_density", "visual_center", "motion_energy"],
            "extra_ai_calls": 0,
            "extra_stock_queries": 0,
        }
    except Exception as exc:
        # Continuity is an editorial preference layered after the already-strict Visual
        # QA selection. A local probe failure must not falsify or bypass that gate.
        return {
            "status": "local_probe_fallback",
            "start_offset_seconds": 0.0,
            "reason": type(exc).__name__,
            "extra_ai_calls": 0,
            "extra_stock_queries": 0,
        }


def _extract_window(source: Path, output: Path, start_seconds: float, seconds: float) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{max(0.0, start_seconds):.3f}", "-i", str(source),
            "-t", f"{max(0.1, seconds):.3f}", "-an",
            "-vf", "fps=30,setsar=1,format=yuv420p",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", str(output),
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=secret_free_subprocess_env(),
    )
    if not output.is_file() or output.stat().st_size <= 1024:
        raise HumanEditorialMontageError("continuity_window_not_usable")
    return output


def _update_json(path: Path, mutate: Callable[[dict[str, Any]], None]) -> None:
    if not path.is_file():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(payload, dict):
        return
    mutate(payload)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


@contextmanager
def _director_montage_scope(root: Path, template: str, state: dict[str, Any]) -> Iterator[None]:
    """Compose local continuity/text choreography into the sealed cinematic director."""
    from scripts import short_cinematic_director as director

    original_prepare = director._prepare_m8_clip
    original_render = director.render_progressive_text
    previous_prepared: Path | None = None

    def prepared_with_continuity(raw: Path, dest: Path, seconds: float):
        nonlocal previous_prepared
        raw_path = Path(raw)
        dest_path = Path(dest)
        previous = previous_prepared
        if previous is None:
            core = dest_path.parent / "shot-01-core.mp4"
            if core.is_file():
                previous = core
        decision = (
            choose_match_offset(previous, raw_path, float(seconds))
            if previous is not None and previous.is_file()
            else {
                "status": "no_previous_shot",
                "start_offset_seconds": 0.0,
                "extra_ai_calls": 0,
                "extra_stock_queries": 0,
            }
        )
        source = raw_path
        temp_window: Path | None = None
        offset = float(decision.get("start_offset_seconds") or 0.0)
        if offset >= 0.04:
            temp_window = dest_path.with_name(dest_path.stem + "-human-window.mp4")
            try:
                source = _extract_window(raw_path, temp_window, offset, float(seconds))
            except Exception as exc:
                decision = {
                    **decision,
                    "status": "window_extract_fallback",
                    "start_offset_seconds": 0.0,
                    "reason": type(exc).__name__,
                }
                source = raw_path
                temp_window = None
        try:
            prepared = original_prepare(source, dest_path, float(seconds))
        finally:
            if temp_window is not None:
                temp_window.unlink(missing_ok=True)
        previous_prepared = Path(prepared)
        decision = {
            **decision,
            "prepared_shot": dest_path.name,
            "source_asset": raw_path.name,
        }
        state.setdefault("continuity", []).append(decision)

        # Keep M8 evidence truthful even when a temporary local trim was used.
        m8_path = dest_path.with_suffix(".m8.json")
        def mutate_m8(payload: dict[str, Any]) -> None:
            payload["source"] = raw_path.name
            payload["human_editorial_montage_v1"] = decision
        _update_json(m8_path, mutate_m8)
        return prepared

    def render_with_choreography(*, video: Path, events, srt_path: Path, output: Path):
        render_events, evidence = choreograph_text_events(list(events), template)
        state["text_choreography"] = evidence
        return original_render(
            video=video,
            events=render_events,
            srt_path=srt_path,
            output=output,
        )

    director._prepare_m8_clip = prepared_with_continuity
    director.render_progressive_text = render_with_choreography
    try:
        yield
    finally:
        director._prepare_m8_clip = original_prepare
        director.render_progressive_text = original_render


def _annotate_visual_timeline(root: Path, state: dict[str, Any]) -> None:
    path = root / "short-visual-timeline.json"
    if not path.is_file():
        return
    try:
        timeline = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(timeline, dict):
        return
    shots = timeline.get("shots") if isinstance(timeline.get("shots"), list) else []
    decisions = list(state.get("continuity") or [])
    for index, decision in enumerate(decisions, start=1):
        if index < len(shots) and isinstance(shots[index], dict):
            shots[index]["local_match_cut"] = decision
    timeline["human_editorial_montage_v1"] = {
        "profile": PROFILE,
        "human_cut_timing": state.get("timing"),
        "visual_continuity": {
            "status": "applied" if decisions else "not_applicable",
            "local_only": True,
            "additional_provider_calls": 0,
            "additional_stock_queries": 0,
            "decisions": decisions,
        },
        "text_choreography": state.get("text_choreography"),
        "transition_policy": "hard_cuts_with_role_template_microtiming",
    }
    path.write_text(json.dumps(timeline, ensure_ascii=False, indent=2), encoding="utf-8")


def _annotate_sfx_report(root: Path, bridge: dict[str, Any], updated: dict[str, Any]) -> None:
    path = root / "short-sfx-plan.json"
    if not path.is_file():
        return
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(report, dict):
        return
    status = str(report.get("status") or "")
    if status == "mixed" and report.get("events"):
        first = report["events"][0]
        if isinstance(first, dict):
            first["reason"] = "short_payoff_pre_lap_sound_bridge"
        report["human_sound_bridge"] = bridge
        bridge["status"] = "mixed"
    else:
        bridge["status"] = f"base_{status or 'not_applicable'}"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    updated["short_sfx"] = report


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def apply_voice_owned_short_human(
    base_apply: Callable[..., dict[str, Any]],
    output_dir: Path,
    control_request: dict[str, Any],
    pre_gold: dict[str, Any],
    *,
    ledger: Any,
) -> dict[str, Any]:
    """Compose five zero-cost human-editorial refinements around the certified owner.

    No provider budget, retry count, Visual QA threshold, rights gate, audio gate or Gold
    gate is changed. The adapter is deliberately inside one lock because it temporarily
    replaces imported call surfaces and restores them in `finally`.
    """
    root = Path(output_dir)
    scope = _clean(control_request.get("approval_scope"))
    template = _clean(pre_gold.get("short_template"))
    if scope not in {"short_only", "short_sibling"}:
        return base_apply(output_dir, control_request, pre_gold, ledger=ledger)
    _template_offsets(template)

    from scripts import short_voice_owned_timeline as voice_owner

    state: dict[str, Any] = {
        "profile": PROFILE,
        "scope": scope,
        "template": template,
        "timing": {"status": "source_derived_visuals_preserved"} if scope != "short_only" else {},
        "continuity": [],
        "text_choreography": {"status": "source_derived_visuals_preserved"} if scope != "short_only" else {},
        "sound_bridge": {},
    }

    with _COMPOSITION_LOCK:
        original_retime = voice_owner.retime_events
        original_upgrade = voice_owner.upgrade_short_cinematic
        original_sfx = voice_owner.apply_short_sfx

        def retime_with_human_rhythm(*args, **kwargs):
            retimed = original_retime(*args, **kwargs)
            if scope != "short_only":
                return retimed
            humanized, evidence = humanize_event_windows(retimed, template)
            state["timing"] = evidence
            return humanized

        def upgrade_with_human_continuity(output_dir, request, current, *, ledger):
            with _director_montage_scope(Path(output_dir), template, state):
                upgraded = original_upgrade(output_dir, request, current, ledger=ledger)
            _annotate_visual_timeline(Path(output_dir), state)
            return upgraded

        def sfx_with_sound_bridge(output_dir, current):
            official_events = [dict(item) for item in list(current.get("timed_text_events") or []) if isinstance(item, dict)]
            bridge_events, bridge = sound_bridge_events(official_events, template)
            staged = dict(current)
            staged["timed_text_events"] = bridge_events
            updated = original_sfx(output_dir, staged)
            updated = dict(updated)
            updated["timed_text_events"] = official_events
            state["sound_bridge"] = bridge
            _annotate_sfx_report(Path(output_dir), bridge, updated)
            return updated

        voice_owner.retime_events = retime_with_human_rhythm
        voice_owner.upgrade_short_cinematic = upgrade_with_human_continuity
        voice_owner.apply_short_sfx = sfx_with_sound_bridge
        try:
            updated = base_apply(output_dir, control_request, pre_gold, ledger=ledger)
        finally:
            voice_owner.retime_events = original_retime
            voice_owner.upgrade_short_cinematic = original_upgrade
            voice_owner.apply_short_sfx = original_sfx

    final_path = root / "final.mp4"
    report = {
        "schema_version": 1,
        "profile": PROFILE,
        "status": "applied",
        "scope": scope,
        "template": template,
        "human_cut_timing": state.get("timing"),
        "visual_continuity_match_cut": {
            "status": "applied" if state.get("continuity") else "source_derived_visuals_preserved",
            "decisions": state.get("continuity"),
            "additional_provider_calls": 0,
            "additional_stock_queries": 0,
        },
        "editorial_rhythm_map": state.get("timing"),
        "selective_text_choreography": state.get("text_choreography"),
        "micro_sound_bridge": state.get("sound_bridge"),
        "hard_cut_default_preserved": True,
        "transition_pack_added": False,
        "speech_speed_changed": False,
        "words_changed": False,
        "extra_text_ai_calls": 0,
        "extra_vision_ai_calls": 0,
        "extra_stock_queries": 0,
        "final_sha256": _sha256_file(final_path) if final_path.is_file() else None,
    }
    (root / "human-editorial-montage-v1.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    updated = dict(updated)
    compensation = dict(updated.get("compensation") or {})
    compensation.update(
        {
            "human_editorial_montage_profile": PROFILE,
            "human_cut_microtiming_applied": scope == "short_only",
            "visual_continuity_local_match_cut": scope == "short_only",
            "selective_text_choreography_applied": scope == "short_only",
            "micro_sound_bridge_reuses_existing_sfx": True,
            "human_montage_extra_ai_calls": 0,
            "human_montage_extra_stock_queries": 0,
            "hard_cut_default_preserved": True,
        }
    )
    updated["compensation"] = compensation
    updated["human_editorial_montage"] = report
    intelligence = root / "short-intelligence-pre-gold.json"
    if intelligence.is_file():
        intelligence.write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8")
    return updated

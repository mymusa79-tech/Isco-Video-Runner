from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from isco_video_agent.short_hook_director import validate_hook_schema
from isco_video_agent.short_identity_gate import evaluate_channel_identity
from isco_video_agent.short_professional_intelligence import (
    choose_length_band,
    evaluate_satisfaction,
    validate_actual_length,
    validate_first_frame,
    validate_promise_payoff,
)
from isco_video_agent.short_quality_gate import evaluate_short_quality_v11
from isco_video_agent.short_timed_text import render_progressive_text, validate_progressive_text
from isco_video_agent.short_topic_gate import evaluate_short_topic

SCHEMA_VERSION = 1
_ALLOWED_SHORT_TEMPLATES = {
    "why_reframe",
    "inner_dialogue",
    "micro_story",
    "quote_reflection",
}
_TEMPLATE_TIMING_WEIGHTS: dict[str, dict[int, tuple[float, ...]]] = {
    "why_reframe": {
        2: (0.34, 0.66),
        3: (0.23, 0.34, 0.43),
        4: (0.20, 0.27, 0.25, 0.28),
    },
    "inner_dialogue": {
        2: (0.40, 0.60),
        3: (0.28, 0.31, 0.41),
        4: (0.24, 0.26, 0.22, 0.28),
    },
    "micro_story": {
        2: (0.34, 0.66),
        3: (0.22, 0.36, 0.42),
        4: (0.18, 0.29, 0.25, 0.28),
    },
    "quote_reflection": {
        2: (0.45, 0.55),
        3: (0.34, 0.25, 0.41),
        4: (0.30, 0.18, 0.22, 0.30),
    },
}
_TEMPLATE_ZOOM_FACTORS: dict[str, tuple[float, ...]] = {
    "why_reframe": (1.00, 1.05, 1.08, 1.03),
    "inner_dialogue": (1.02, 1.06, 1.03, 1.08),
    "micro_story": (1.00, 1.04, 1.07, 1.10),
    "quote_reflection": (1.00, 1.03, 1.00, 1.05),
}
_TEMPLATE_MOTION_LABELS: dict[str, tuple[str, ...]] = {
    "why_reframe": ("hold", "push_in", "closer_reframe", "release"),
    "inner_dialogue": ("intimate_hold", "pressure_push", "breath_reset", "resolve_push"),
    "micro_story": ("establish", "advance", "turn", "meaning_push"),
    "quote_reflection": ("quote_hold", "gentle_push", "reset", "payoff_push"),
}


def _read(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise RuntimeError(f"Shorts binding missing required artifact: {path.name}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Shorts binding invalid JSON artifact: {path.name}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"Shorts binding artifact must be an object: {path.name}")
    return data


def _unique_texts(plan: dict[str, Any]) -> list[str]:
    sections = plan.get("sections") if isinstance(plan.get("sections"), list) else []
    first = sections[0] if sections and isinstance(sections[0], dict) else {}
    title_options = plan.get("title_options") if isinstance(plan.get("title_options"), list) else []
    values = [
        str(plan.get("hook") or ""),
        str(title_options[0] if title_options else plan.get("topic") or ""),
        str(first.get("on_screen_text") or first.get("key_point") or ""),
        str(plan.get("closing_payoff") or ""),
    ]
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = " ".join(value.strip().split())
        key = text.casefold().strip(" .،!?؟")
        if not text or not key or key in seen:
            continue
        seen.add(key)
        out.append(text[:220])
    if len(out) < 2:
        raise RuntimeError("Shorts binding requires at least two distinct semantic text beats")
    if len(out) > 4:
        out = [out[0], *out[1:3], out[-1]]
    return out


def _duration(quality: dict[str, Any]) -> float:
    for key in ("video_stream_duration", "duration_seconds", "duration"):
        try:
            value = float(quality.get(key) or 0.0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    raise RuntimeError("Shorts binding cannot resolve final duration")


def _short_template(plan: dict[str, Any], control_request: dict[str, Any]) -> str:
    scope = str(control_request.get("approval_scope") or "").strip()
    if scope == "short_only":
        editorial = plan.get("editorial_intent") if isinstance(plan.get("editorial_intent"), dict) else {}
        template = str(editorial.get("short_template") or "").strip()
        if template not in _ALLOWED_SHORT_TEMPLATES:
            raise RuntimeError("Standalone Short is missing a valid topic-selected template")
        return template
    if scope == "short_sibling":
        source_plan = control_request.get("source_short_plan")
        template = str((source_plan or {}).get("template") or "").strip() if isinstance(source_plan, dict) else ""
        if template not in _ALLOWED_SHORT_TEMPLATES:
            raise RuntimeError("Source-derived Short is missing a valid inherited template")
        return template
    raise RuntimeError("Shorts compensation requires short_only or short_sibling approval scope")


def _timing_boundaries(total_ms: int, count: int, template: str) -> list[int]:
    try:
        weights = _TEMPLATE_TIMING_WEIGHTS[template][count]
    except KeyError as exc:
        raise RuntimeError("Shorts compensation has no timing profile for this template/beat count") from exc
    boundaries = [0]
    elapsed = 0.0
    for weight in weights[:-1]:
        elapsed += weight
        boundaries.append(round(total_ms * elapsed))
    boundaries.append(total_ms)
    return boundaries


def _hook_and_text_contract(
    texts: list[str],
    duration_s: float,
    template: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    count = len(texts)
    total_ms = max(count, int(round(duration_s * 1000)))
    boundaries = _timing_boundaries(total_ms, count, template)
    beats: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    for index, text in enumerate(texts):
        start_ms = int(boundaries[index])
        end_ms = int(boundaries[index + 1])
        role = "hook" if index == 0 else ("payoff" if index == count - 1 else "beat")
        beats.append(
            {
                "beat_id": f"b{index + 1:02d}",
                "start_ms": start_ms,
                "end_ms": end_ms,
                "semantic_job": role,
                "hook_commit": index == 0,
                "template": template,
            }
        )
        events.append(
            {
                "start": round(start_ms / 1000.0, 3),
                "end": round(end_ms / 1000.0, 3),
                "text": text,
                "role": role,
                "template": template,
            }
        )
    hook = validate_hook_schema({"beats": beats, "hook_commit_ms": 0})
    validate_progressive_text(events)
    return hook, events


def _video_dimensions(video: Path) -> tuple[int, int]:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "json",
        str(video),
    ]
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    data = json.loads(result.stdout or "{}")
    streams = data.get("streams") if isinstance(data, dict) else None
    first = streams[0] if isinstance(streams, list) and streams and isinstance(streams[0], dict) else {}
    width = int(first.get("width") or 0)
    height = int(first.get("height") or 0)
    if width <= 0 or height <= 0:
        raise RuntimeError("Shorts compensation cannot resolve picture dimensions")
    return width, height


def _beat_directives(template: str, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    factors = _TEMPLATE_ZOOM_FACTORS[template]
    labels = _TEMPLATE_MOTION_LABELS[template]
    directives: list[dict[str, Any]] = []
    for index, event in enumerate(events):
        directives.append(
            {
                "beat_id": f"b{index + 1:02d}",
                "role": event["role"],
                "start": event["start"],
                "end": event["end"],
                "visual_change": True,
                "motion_intent": labels[index],
                "zoom_factor": factors[index],
                "audio_cue": "none_generated_preserve_existing_mix",
            }
        )
    return directives


def _apply_beat_reframes(
    video: Path,
    events: list[dict[str, Any]],
    template: str,
    output: Path,
) -> Path:
    """Create restrained beat-synchronised visual changes without another media/AI call."""
    width, height = _video_dimensions(video)
    factors = _TEMPLATE_ZOOM_FACTORS[template]
    filters: list[str] = []
    labels: list[str] = []
    for index, event in enumerate(events):
        start = float(event["start"])
        end = float(event["end"])
        if end <= start:
            raise RuntimeError("Shorts compensation received a non-positive beat duration")
        factor = factors[index]
        scaled_width = max(width, int(round(width * factor / 2.0) * 2))
        scaled_height = max(height, int(round(height * factor / 2.0) * 2))
        x = max(0, (scaled_width - width) // 2)
        y = max(0, (scaled_height - height) // 2)
        label = f"v{index}"
        filters.append(
            f"[0:v]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS,"
            f"scale={scaled_width}:{scaled_height}:flags=lanczos,"
            f"crop={width}:{height}:{x}:{y},setsar=1[{label}]"
        )
        labels.append(f"[{label}]")
    filters.append(f"{''.join(labels)}concat=n={len(labels)}:v=1:a=0[outv]")
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(video),
        "-filter_complex",
        ";".join(filters),
        "-map",
        "[outv]",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(output),
    ]
    subprocess.run(command, check=True, capture_output=True)
    if not output.is_file() or output.stat().st_size <= 1024:
        raise RuntimeError("Shorts beat compensation did not produce a usable picture")
    return output


def _remux_progressive_video(video: Path, audio_source: Path, output: Path) -> Path:
    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(video),
        "-i",
        str(audio_source),
        "-map",
        "0:v:0",
        "-map",
        "1:a?",
        "-c:v",
        "copy",
        "-c:a",
        "copy",
        "-shortest",
        str(output),
    ]
    subprocess.run(command, check=True, capture_output=True)
    if not output.is_file() or output.stat().st_size <= 1024:
        raise RuntimeError("Shorts progressive remux did not produce a usable final video")
    return output


def prepare_short_render(output_dir: Path, control_request: dict[str, Any]) -> dict[str, Any]:
    """Apply Short-native template, rhythm, hook/text and standalone visual compensation before Gold."""
    root = Path(output_dir)
    if control_request.get("kind") != "short" or control_request.get("approved_by_user") is not True:
        raise RuntimeError("Shorts production requires an explicit approved short control request")
    if control_request.get("production_dispatch_authorized") is not True:
        raise RuntimeError("Shorts production request is not authorized for dispatch")

    plan = _read(root / "plan.json")
    quality = _read(root / "quality-final.json")
    if str(plan.get("format") or quality.get("format") or "") != "moment":
        raise RuntimeError("Shorts production binding requires the 9:16 moment render path")

    topic_result = evaluate_short_topic(control_request.get("short_admission") or {})
    if topic_result.get("decision") != "pass":
        raise RuntimeError("Shorts topic admission blocked the approved candidate")

    template = _short_template(plan, control_request)
    duration_s = _duration(quality)
    texts = _unique_texts(plan)
    hook, events = _hook_and_text_contract(texts, duration_s, template)
    length = choose_length_band(estimated_spoken_seconds=duration_s, beat_count=len(texts))
    if length.get("length_fit_pass") is not True:
        raise RuntimeError("Shorts professional length recommendation blocked the render")
    validate_actual_length(actual_duration_s=duration_s, recommendation=length)

    sections = plan.get("sections") or []
    first_section = sections[0] if sections and isinstance(sections[0], dict) else {}
    reframe = float(topic_result.get("reframe_score") or 0.0)
    knowledge = float(topic_result.get("knowledge_gap_score") or 0.0)
    tension_type = "reframe" if reframe >= knowledge else "knowledge_gap"
    first_event_ms = max(1, int(round((events[0]["end"] - events[0]["start"]) * 1000)))
    first_frame = validate_first_frame(
        {
            "first_frame_end_ms": min(1000, first_event_ms),
            "hook_commit_ms": hook["hook_commit_ms"],
            "tension_type": tension_type,
            "text": texts[0],
            "visual_cue": str(first_section.get("visual_query") or ""),
            "logo_or_intro_before_hook": False,
        }
    )

    picture = root / "picture.mp4"
    existing_final = root / "final.mp4"
    if not picture.is_file() or not existing_final.is_file():
        raise RuntimeError("Shorts progressive render requires picture.mp4 and final.mp4")

    scope = str(control_request.get("approval_scope") or "").strip()
    standalone_compensation = scope == "short_only"
    directives = _beat_directives(template, events)
    compensation_picture = root / "picture-short-comp-v2.mp4"
    progressive_input = picture
    if standalone_compensation:
        progressive_input = _apply_beat_reframes(picture, events, template, compensation_picture)

    compensation = {
        "schema_version": 1,
        "profile": "short_only_compensation_v2",
        "scope": scope,
        "template": template,
        "topic_selected_template": scope == "short_only",
        "beat_driven_timing_applied": True,
        "beat_driven_visual_reframe_applied": standalone_compensation,
        "multi_asset_broll_generated": False,
        "voice_generated": False,
        "existing_audio_preserved": True,
        "extra_ai_calls": 0,
        "directives": directives,
    }
    (root / "short-compensation-plan.json").write_text(
        json.dumps(compensation, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    progressive_picture = root / "picture-short-v1.mp4"
    progressive_final = root / "final-short-v1.mp4"
    render_progressive_text(
        video=progressive_input,
        events=events,
        srt_path=root / "short-progressive.srt",
        output=progressive_picture,
    )
    _remux_progressive_video(progressive_picture, existing_final, progressive_final)
    shutil.move(str(progressive_final), str(existing_final))

    context = {
        "schema_version": SCHEMA_VERSION,
        "stage": "pre_gold",
        "request_id": control_request.get("request_id"),
        "request_sha256": control_request.get("request_sha256"),
        "short_template": template,
        "topic_admission": topic_result,
        "hook_contract": hook,
        "timed_text_events": events,
        "first_frame": first_frame,
        "length_recommendation": length,
        "compensation": compensation,
        "rendered_from": progressive_input.name,
        "progressive_text_applied": True,
        "extra_ai_calls": 0,
    }
    (root / "short-intelligence-pre-gold.json").write_text(
        json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return context


def _accepted_gold(gold: dict[str, Any]) -> bool:
    return bool(
        gold.get("phase") == "4"
        and gold.get("mode") == "enforce"
        and isinstance(gold.get("gold"), dict)
        and gold["gold"].get("accepted") is True
        and isinstance(gold.get("same_render"), dict)
        and gold["same_render"].get("artifact_divergence") is False
    )


def _score01(model_review: dict[str, Any], field: str) -> float:
    value = model_review.get(field)
    if isinstance(value, bool):
        raise RuntimeError(f"Final Critic score is invalid: {field}")
    try:
        score = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Final Critic score is missing or invalid: {field}") from exc
    if score < 0.0 or score > 1.0:
        raise RuntimeError(f"Final Critic score is outside 0..1: {field}")
    return score


def _final_critic_evidence(critic: dict[str, Any]) -> dict[str, float]:
    model_review = critic.get("model_review")
    if critic.get("status") != "pass" or not isinstance(model_review, dict) or model_review.get("status") != "pass":
        raise RuntimeError("Shorts final quality requires a passing Final Critic")
    hard_blocks = critic.get("hard_blocks")
    critical = model_review.get("critical_issues")
    if hard_blocks or critical:
        raise RuntimeError("Shorts final quality cannot inherit a blocked Final Critic")
    names = (
        "human_feel",
        "language_quality",
        "opening_strength",
        "narrative_progression",
        "cultural_fit",
        "monetization_safety",
    )
    return {name: _score01(model_review, name) for name in names}


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    lowered = " ".join(text.casefold().split())
    return any(needle in lowered for needle in needles)


def _satisfaction_flags(plan: dict[str, Any], scores: dict[str, float], *, factual_pass: bool, content_pass: bool) -> dict[str, bool]:
    whole = json.dumps(plan, ensure_ascii=False)
    fake_loop = _contains_any(
        whole,
        (
            "شاهد للنهاية",
            "انتظر للنهاية",
            "لا تمرر",
            "watch until the end",
            "wait until the end",
        ),
    )
    manipulative_cta = _contains_any(
        whole,
        (
            "اشترك الآن وإلا",
            "اضغط الآن قبل",
            "subscribe now or",
            "click now before",
        ),
    )
    return {
        "hook_truthful": factual_pass and scores["opening_strength"] >= 0.70,
        "standalone_value": content_pass and scores["human_feel"] >= 0.72 and scores["narrative_progression"] >= 0.72,
        "payoff_complete": content_pass and scores["narrative_progression"] >= 0.72,
        "fake_loop": fake_loop,
        "manipulative_cta": manipulative_cta,
    }


def finalize_short_quality(output_dir: Path, control_request: dict[str, Any], pre_gold: dict[str, Any]) -> dict[str, Any]:
    root = Path(output_dir)
    plan = _read(root / "plan.json")
    quality = _read(root / "quality-final.json")
    factual = _read(root / "factuality-audit.json")
    content = _read(root / "content-quality-audit.json")
    tone = _read(root / "tone-quality-audit.json")
    rights = _read(root / "rights-manifest.json")
    gold = _read(root / "gold-enforce-report.json")
    critic = _read(root / "final-critic.json")

    gold_pass = _accepted_gold(gold)
    factual_pass = factual.get("status") == "pass"
    content_pass = content.get("status") == "pass"
    tone_pass = tone.get("status") == "pass"
    rights_pass = bool(rights.get("visuals"))
    if not (gold_pass and factual_pass and content_pass and tone_pass and rights_pass):
        raise RuntimeError("Shorts final quality cannot project existing hard gates as passed")

    scores = _final_critic_evidence(critic)
    identity_voice = round(scores["human_feel"] * 10.0, 3)
    identity_tone = round(min(scores["language_quality"], scores["cultural_fit"]) * 10.0, 3)
    identity = evaluate_channel_identity(
        {
            "story_bible_ref": "config/story_bible.yaml",
            "visual_bible_ref": "config/visual_bible.yaml",
            "channel_persona_ref": "config/channel_persona.json",
            "channel_voice_match_score": identity_voice,
            "tone_consistency_score": identity_tone,
        }
    )
    if identity.get("decision") != "pass":
        raise RuntimeError("Shorts identity admission blocked final output")

    duration_s = _duration(quality)
    events = pre_gold.get("timed_text_events") or []
    hook_text = str(events[0].get("text") or "") if events else ""
    payoff_text = str(events[-1].get("text") or "") if events else ""
    payoff_start = float(events[-1].get("start") or 0.0) if events else 0.0
    payoff_end = float(events[-1].get("end") or duration_s) if events else duration_s
    promise_match_score = round(min(scores["opening_strength"], scores["narrative_progression"]) * 10.0, 3)
    promise = validate_promise_payoff(
        {
            "hook_promise": hook_text,
            "payoff": payoff_text,
            "promise_payoff_match_score": promise_match_score,
            "duration_s": duration_s,
            "payoff_start_s": payoff_start,
            "payoff_end_s": payoff_end,
        }
    )
    satisfaction_inputs = _satisfaction_flags(
        plan,
        scores,
        factual_pass=factual_pass,
        content_pass=content_pass,
    )
    satisfaction = evaluate_satisfaction(satisfaction_inputs)
    length = validate_actual_length(
        actual_duration_s=duration_s,
        recommendation=pre_gold["length_recommendation"],
    )

    safety_pass = gold_pass and scores["monetization_safety"] >= 0.90
    cultural_pass = tone_pass and scores["cultural_fit"] >= 0.90
    evidence = {
        "safety_pass": safety_pass,
        "cultural_pass": cultural_pass,
        "islamic_pass": cultural_pass,
        "factual_pass": factual_pass,
        "rights_pass": rights_pass,
        "content_quality_pass": content_pass,
        "topic_admission_pass": pre_gold["topic_admission"].get("decision") == "pass",
        "identity_admission_pass": identity.get("decision") == "pass",
        "single_action_pass": bool(pre_gold["topic_admission"].get("single_action_contract")),
        "hook_contract_pass": pre_gold["hook_contract"].get("hook_commit_ms", 999999) <= 3000,
        "progressive_text_pass": pre_gold.get("progressive_text_applied") is True,
        "payoff_pass": promise.get("decision") == "pass",
        "first_frame_contract_pass": pre_gold["first_frame"].get("decision") == "pass",
        "promise_payoff_pass": promise.get("decision") == "pass",
        "satisfaction_pass": satisfaction.get("decision") == "pass",
        "length_fit_pass": length.get("decision") == "pass",
        "hook_commit_ms": pre_gold["hook_contract"]["hook_commit_ms"],
        "beat_count": len(pre_gold.get("timed_text_events") or []),
    }
    quality_result = evaluate_short_quality_v11(evidence)
    if quality_result.get("decision") != "pass":
        raise RuntimeError("Shorts V1.1 final quality gate blocked delivery")

    report = {
        "schema_version": SCHEMA_VERSION,
        "stage": "final",
        "request_id": control_request.get("request_id"),
        "topic": str(plan.get("topic") or ""),
        "quality_profile": "shorts_v1_1_professional",
        "compensation_profile": "short_only_compensation_v2",
        "short_template": pre_gold.get("short_template"),
        "short_compensation": pre_gold.get("compensation"),
        "topic_admission": pre_gold["topic_admission"],
        "identity_admission": identity,
        "hook_contract": pre_gold["hook_contract"],
        "first_frame": pre_gold["first_frame"],
        "promise_payoff": promise,
        "satisfaction": satisfaction,
        "length": length,
        "quality_gate": quality_result,
        "evidence_provenance": {
            "source": "final-critic.json model_review + existing hard gates",
            "synthetic_perfect_scores": False,
            "identity_voice_score_source": "final_critic.model_review.human_feel",
            "identity_tone_score_source": "min(language_quality,cultural_fit)",
            "promise_payoff_score_source": "min(opening_strength,narrative_progression)",
            "final_critic_scores_0_to_1": scores,
        },
        "gold_inherited": True,
        "delivery_allowed": True,
        "youtube_publish_mode": "manual_in_youtube_studio",
    }
    (root / "short-intelligence.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report

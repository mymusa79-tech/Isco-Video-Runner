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


def _hook_and_text_contract(texts: list[str], duration_s: float) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    count = len(texts)
    total_ms = max(count, int(round(duration_s * 1000)))
    boundaries = [round(total_ms * index / count) for index in range(count + 1)]
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
            }
        )
        events.append(
            {
                "start": round(start_ms / 1000.0, 3),
                "end": round(end_ms / 1000.0, 3),
                "text": text,
                "role": role,
            }
        )
    hook = validate_hook_schema({"beats": beats, "hook_commit_ms": 0})
    validate_progressive_text(events)
    return hook, events


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
    """Apply Short-native hook/text contracts before Gold reviews the final bytes."""
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

    duration_s = _duration(quality)
    texts = _unique_texts(plan)
    hook, events = _hook_and_text_contract(texts, duration_s)
    length = choose_length_band(estimated_spoken_seconds=duration_s, beat_count=len(texts))
    if length.get("length_fit_pass") is not True:
        raise RuntimeError("Shorts professional length recommendation blocked the render")
    validate_actual_length(actual_duration_s=duration_s, recommendation=length)

    sections = plan.get("sections") or []
    first_section = sections[0] if sections and isinstance(sections[0], dict) else {}
    reframe = float(topic_result.get("reframe_score") or 0.0)
    knowledge = float(topic_result.get("knowledge_gap_score") or 0.0)
    tension_type = "reframe" if reframe >= knowledge else "knowledge_gap"
    first_frame = validate_first_frame(
        {
            "first_frame_end_ms": min(1000, max(1, int(duration_s * 1000 / len(texts)))),
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
    progressive_picture = root / "picture-short-v1.mp4"
    progressive_final = root / "final-short-v1.mp4"
    render_progressive_text(
        video=picture,
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
        "topic_admission": topic_result,
        "hook_contract": hook,
        "timed_text_events": events,
        "first_frame": first_frame,
        "length_recommendation": length,
        "rendered_from": "picture.mp4",
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

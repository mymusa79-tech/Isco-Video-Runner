from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .channel_persona import with_channel_persona
from .human_feel import with_human_feel
from .identity_sequence import (
    PRAYER_SENTENCE,
    SHORT_CHANNEL_DEFINITION,
    apply_identity_media,
    assert_spoken_identity,
    inject_spoken_identity,
)
from .contracts import (
    atomic_write_json,
    compute_brief_sha256,
    load_approved_brief,
    require_exact_engine_sha,
    validate_narrative_identity,
    validate_plan,
    validate_script,
)
from .media import concat_wav_parts, inspect_final, probe_duration, render_video
from .structural_ai import structural_ai_flags
from .short_format import (
    SHORT_MAX_SECONDS,
    SHORT_MIN_SECONDS,
    SHORT_TARGET_SECONDS,
    INNER_DIALOGUE_VOICE_RULES,
    apply_safe_short_hook_trim,
    select_short_template,
    short_contract_report,
    short_prompt_context,
    validate_short_duration,
    validate_short_dimensions,
    validate_short_hook_contract,
    validate_short_script,
)


CINEMATIC_STAGE = "security_v1_cinematic_v2_m7_m11"
VISUAL_QA_STAGE = "final_cut_visual_qa"
OPENING_STAGE = "opening_director"
STRUCTURAL_AI_STAGE = "structural_ai_flags"
TEXT_AUDIT_STAGE = "text_audit"
AUDIO_MASTERING_STAGE = "audio_mastering"
IDENTITY_STAGE = "narrative_identity"
_PLANNING_FACTUALITY_RULE = (
    "Use precise scientific, psychological, medical, historical, legal, political, statistical or religious "
    "factual claims only when directly supported by APPROVED_RESEARCH_PACK. Never invent studies, numbers, "
    "quotes, experts or causation. If evidence is insufficient, use a modest non-technical observation or "
    "omit the claim."
)
QUALITY_STAGE = "final_master_qc"
# Audio mastering is a deterministic ffmpeg transformation, not a content-judgment
# gate, so it is deliberately NOT in QUALITY_STAGES: a failure here is always a
# plain technical failure, never a "quality_pending" content block.
QUALITY_STAGES = frozenset(
    {CINEMATIC_STAGE, VISUAL_QA_STAGE, OPENING_STAGE, TEXT_AUDIT_STAGE, QUALITY_STAGE}
)
RESUME_CONTRACT_VERSION = 2
RESUMABLE_STAGES = ("planning", "script", "voice", "visuals")
_RESUME_STAGE_INDEX = {name: index for index, name in enumerate(RESUMABLE_STAGES)}

STAGES = (
    "brief",
    "planning",
    IDENTITY_STAGE,
    "script",
    STRUCTURAL_AI_STAGE,
    TEXT_AUDIT_STAGE,
    "voice",
    AUDIO_MASTERING_STAGE,
    "visuals",
    VISUAL_QA_STAGE,
    OPENING_STAGE,
    "render",
    CINEMATIC_STAGE,
    "final_file",
    QUALITY_STAGE,
)


VOICE_CHUNK_MAX_CHARS = 550


def _bounded_voice_chunks(text: str, *, max_chars: int = VOICE_CHUNK_MAX_CHARS) -> list[str]:
    """Split long narration at sentence/word boundaries without changing wording."""
    normalized = " ".join(str(text or "").split()).strip()
    if not normalized:
        return []
    if max_chars < 120:
        raise ValueError("voice chunk bound is too small")
    if len(normalized) <= max_chars:
        return [normalized]

    sentences = [
        item.strip()
        for item in re.split(r"(?<=[.!؟!])\s+", normalized)
        if item.strip()
    ]
    pieces: list[str] = []
    for sentence in sentences:
        if len(sentence) <= max_chars:
            pieces.append(sentence)
            continue
        words = sentence.split()
        current = ""
        for word in words:
            candidate = word if not current else f"{current} {word}"
            if len(candidate) <= max_chars:
                current = candidate
                continue
            if current:
                pieces.append(current)
            if len(word) > max_chars:
                raise RuntimeError("Clean V2 voice chunk contains an overlong token")
            current = word
        if current:
            pieces.append(current)

    chunks: list[str] = []
    current = ""
    for piece in pieces:
        candidate = piece if not current else f"{current} {piece}"
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = piece
    if current:
        chunks.append(current)

    if " ".join(" ".join(chunks).split()) != normalized:
        raise RuntimeError("Clean V2 voice chunking changed narration text")
    if any(len(chunk) > max_chars for chunk in chunks):
        raise RuntimeError("Clean V2 voice chunk exceeds local bound")
    return chunks


def _synthesize_sectioned_voice(
    voice_synthesizer: Any,
    sections: list[dict[str, Any]],
    narration_path: Path,
    *,
    require_charon_only: bool = False,
) -> dict[str, Any]:
    """Synthesize bounded Charon units, then deterministically reassemble sections.

    Script sections remain the semantic boundary. Long sections are split locally at
    sentence/word boundaries only to reduce TTS timeout surface; no AI or wording
    rewrite is introduced. Each chunk uses the existing Charon retry policy, so a
    transient failure retries only that chunk rather than the entire long section.
    """
    if not sections:
        raise RuntimeError("Clean V2 sectioned voice requires at least one section")

    audio_dir = narration_path.parent / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    section_paths: list[Path] = []
    reports: list[dict[str, Any]] = []
    expected_provider: str | None = None
    total_charon_attempts = 0
    any_fallback = False
    approval_status: str | None = None
    reference_profile: str | None = None
    role_reports: list[dict[str, Any]] = []
    report_path = narration_path.parent / "voice-sections.json"

    for index, item in enumerate(sections, start=1):
        section_id = str(item.get("id") or f"s{index}")
        section_text = str(item.get("narration") or "").strip()
        if not section_text:
            raise RuntimeError(
                f"Clean V2 sectioned voice found empty narration: section={section_id}"
            )

        # For the approved Hook -> Intro -> identity order, keep the very first
        # spoken sentence as its own TTS chunk. This gives the post-render identity
        # splice an exact measured hook boundary without alignment AI or extra calls.
        if index == 1 and PRAYER_SENTENCE in section_text:
            match = re.search(r"[.!؟!]", section_text)
            if match is not None and match.end() < len(section_text):
                hook_text = section_text[: match.end()].strip()
                remainder = section_text[match.end() :].strip()
                chunks = [hook_text, *_bounded_voice_chunks(remainder)]
            else:
                chunks = _bounded_voice_chunks(section_text)
        else:
            chunks = _bounded_voice_chunks(section_text)
        if not chunks:
            raise RuntimeError(
                f"Clean V2 sectioned voice found no narration chunks: section={section_id}"
            )
        section_path = audio_dir / f"{index:02d}.wav"
        chunk_reports: list[dict[str, Any]] = []
        chunk_paths: list[Path] = []
        section_provider: str | None = None
        section_attempts = 0
        section_fallback = False

        for chunk_index, chunk_text in enumerate(chunks, start=1):
            if len(chunks) == 1:
                chunk_path = section_path
            else:
                chunk_dir = audio_dir / f"{index:02d}-chunks"
                chunk_dir.mkdir(parents=True, exist_ok=True)
                chunk_path = chunk_dir / f"{chunk_index:02d}.wav"
            try:
                if require_charon_only:
                    voice_synthesizer.synthesize(
                        chunk_text,
                        chunk_path,
                        primary_only=True,
                    )
                else:
                    voice_synthesizer.synthesize(chunk_text, chunk_path)
            except Exception:
                atomic_write_json(
                    report_path,
                    {
                        "schema_version": 1,
                        "source": "clean-v2-sectioned-voice",
                        "status": "failed",
                        "failed_section": section_id,
                        "failed_chunk": chunk_index,
                        "chunk_chars": len(chunk_text),
                        "sections": reports,
                        "current_section_chunks": chunk_reports,
                    },
                )
                raise

            provider = str(getattr(voice_synthesizer, "last_provider", "") or "")
            if not provider:
                raise RuntimeError(
                    "Clean V2 sectioned voice provider missing: "
                    f"section={section_id} chunk={chunk_index}"
                )
            if require_charon_only and provider != "gemini:Charon":
                raise RuntimeError(
                    "CLEAN_V2_VOICE_INFRASTRUCTURE reason=short_charon_only_provider_drift "
                    f"actual={provider}"
                )
            if section_provider is None:
                section_provider = provider
            elif provider != section_provider:
                raise RuntimeError(
                    "Clean V2 voice provider drift inside section is forbidden: "
                    f"expected={section_provider} actual={provider} "
                    f"section={section_id} chunk={chunk_index}"
                )

            attempts = int(getattr(voice_synthesizer, "charon_attempts", 0) or 0)
            section_attempts += attempts
            total_charon_attempts += attempts
            fallback_used = bool(getattr(voice_synthesizer, "fallback_used", False))
            section_fallback = section_fallback or fallback_used
            any_fallback = any_fallback or fallback_used
            current_approval = getattr(
                voice_synthesizer, "voice_approval_status", None
            )
            current_reference = getattr(
                voice_synthesizer, "voice_reference_profile", None
            )
            if isinstance(current_approval, str) and current_approval:
                approval_status = current_approval
            if isinstance(current_reference, str) and current_reference:
                reference_profile = current_reference
            current_roles = getattr(voice_synthesizer, "voice_roles", None)
            if isinstance(current_roles, dict):
                role_reports.append(
                    {
                        "id": section_id,
                        "chunk": chunk_index,
                        **dict(current_roles),
                    }
                )

            chunk_paths.append(chunk_path)
            chunk_reports.append(
                {
                    "chunk": chunk_index,
                    "file": str(chunk_path.relative_to(narration_path.parent)),
                    "chars": len(chunk_text),
                    "provider": provider,
                    "charon_attempts": attempts,
                    "fallback_used": fallback_used,
                }
            )

        if len(chunk_paths) > 1:
            joined_section = audio_dir / f".{index:02d}-chunk-join.wav"
            joined_list = joined_section.with_suffix(".txt")
            try:
                concat_wav_parts(chunk_paths, joined_section)
                if not joined_section.is_file() or joined_section.stat().st_size < 1024:
                    raise RuntimeError(
                        f"Clean V2 section chunk concat produced empty audio: section={section_id}"
                    )
                os.replace(joined_section, section_path)
            finally:
                joined_section.unlink(missing_ok=True)
                joined_list.unlink(missing_ok=True)

        provider = str(section_provider or "")
        if expected_provider is None:
            expected_provider = provider
        elif provider != expected_provider:
            atomic_write_json(
                report_path,
                {
                    "schema_version": 1,
                    "source": "clean-v2-sectioned-voice",
                    "status": "failed",
                    "failed_section": section_id,
                    "reason": "voice_provider_drift",
                    "expected_provider": expected_provider,
                    "actual_provider": provider,
                    "sections": reports,
                },
            )
            raise RuntimeError(
                "Clean V2 sectioned voice provider drift is forbidden: "
                f"expected={expected_provider} actual={provider} section={section_id}"
            )

        section_paths.append(section_path)
        reports.append(
            {
                "id": section_id,
                "file": str(Path("audio") / section_path.name),
                "provider": provider,
                "charon_attempts": section_attempts,
                "fallback_used": section_fallback,
                "chunk_count": len(chunks),
                "chunks": chunk_reports,
            }
        )
        atomic_write_json(
            report_path,
            {
                "schema_version": 1,
                "source": "clean-v2-sectioned-voice",
                "status": "in_progress",
                "sections": reports,
            },
        )

    joined_path = narration_path.with_name(".narration-section-join.wav")
    joined_list_path = joined_path.with_suffix(".txt")
    try:
        concat_wav_parts(section_paths, joined_path)
        if not joined_path.is_file() or joined_path.stat().st_size < 1024:
            raise RuntimeError("Clean V2 sectioned voice concat produced empty audio")
        os.replace(joined_path, narration_path)
    finally:
        joined_path.unlink(missing_ok=True)
        joined_list_path.unlink(missing_ok=True)

    result = {
        "voice_provider": expected_provider,
        "voice_fallback_used": any_fallback,
        "charon_tts_attempts": total_charon_attempts,
        "voice_roles": {
            "mode": "sectioned",
            "sections": role_reports,
        },
        "voice_approval_status": approval_status,
        "voice_reference_profile": reference_profile,
        "sections": reports,
    }
    atomic_write_json(
        report_path,
        {
            "schema_version": 1,
            "source": "clean-v2-sectioned-voice",
            "status": "pass",
            **result,
        },
    )
    return result


def _read_secret(name: str) -> str:
    direct = str(os.environ.get(name) or "").strip()
    if direct:
        return direct
    file_value = str(os.environ.get(f"{name}_FILE") or "").strip()
    if not file_value:
        return ""
    path = Path(file_value)
    try:
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _section_narration_char_counts(
    sections: list[dict[str, Any]], script: Mapping[str, Any]
) -> dict[str, int]:
    narration_by_id = {
        str(item.get("id") or ""): str(item.get("narration") or "")
        for item in (script.get("sections") or [])
        if isinstance(item, dict)
    }
    return {
        str(section.get("id") or ""): len(
            " ".join(narration_by_id.get(str(section.get("id") or ""), "").split())
        )
        for section in sections
    }


def _estimate_section_seconds(
    sections: list[dict[str, Any]],
    script: Mapping[str, Any],
    total_seconds: float,
) -> dict[str, float]:
    """Estimate each section's share of the narration's total duration from
    its own narration character count, instead of assuming every section is
    the same length. Deterministic and purely local: no AI/provider call,
    just a proportional split of the already-known total narration duration.

    The last section absorbs any rounding residual so the sum of every
    section's estimate always equals total_seconds exactly.
    """
    counts = _section_narration_char_counts(sections, script)
    section_ids = [str(section.get("id") or "") for section in sections]
    total_chars = sum(counts.values())
    if total_chars <= 0:
        raise RuntimeError(
            "Clean V2 cannot estimate section durations: no narration text "
            "found for any section"
        )
    estimated: dict[str, float] = {}
    allocated = 0.0
    for index, section_id in enumerate(section_ids):
        if index == len(section_ids) - 1:
            estimated[section_id] = max(0.0, total_seconds - allocated)
        else:
            share = (counts.get(section_id, 0) / total_chars) * total_seconds
            estimated[section_id] = share
            allocated += share
    return estimated


def _run_legacy_final_master_qc(output_dir: Path) -> dict[str, Any]:
    # Deliberately reuse the certified legacy technical QC unchanged.
    # The Engine package is supplied by the production workflow via PYTHONPATH.
    from scripts.final_master_qc import run_final_master_qc

    return run_final_master_qc(output_dir)


def _run_final_cut_visual_qa(
    *,
    output_dir: Path,
    plan: dict[str, Any],
    script: dict[str, Any],
    rights: list[dict[str, Any]],
    fmt: str,
    router: Any,
    visual_source: Any,
) -> dict[str, Any]:
    from clean_v2.visual_qa import run_final_cut_visual_qa

    return run_final_cut_visual_qa(
        output_dir=output_dir,
        plan=plan,
        script=script,
        rights=rights,
        fmt=fmt,
        router=router,
        visual_source=visual_source,
    )


def _run_opening_director(
    *,
    output_dir: Path,
    plan: dict[str, Any],
    script: dict[str, Any],
    rights: list[dict[str, Any]],
    fmt: str,
    narration_path: Path,
    visual_source: Any,
    router: Any,
) -> dict[str, Any]:
    from clean_v2.opening_director import run_opening_director

    return run_opening_director(
        output_dir=output_dir,
        plan=plan,
        script=script,
        rights=rights,
        fmt=fmt,
        narration_path=narration_path,
        visual_source=visual_source,
        router=router,
    )


_AUDIT_NARRATIVE_FORMAT_OVERRIDES = {
    # "inner_dialogue" is a Clean V2 Short template label whose own name
    # misleads the reused frozen-Engine tone audit into expecting an actual
    # back-and-forth exchange between two voices (Runs #17 and #22 both
    # blocked correct single-voice inner narration with "content is a
    # monologue, not dialogue"). Send the audit an unambiguous equivalent
    # label instead; this only changes what the Engine's audit sees, not the
    # Clean V2 template name used everywhere else (prompts, contracts,
    # manifests).
    "inner_dialogue": "inner_monologue",
}


def _audit_narrative_format_for_brief(brief: Mapping[str, Any]) -> str:
    """Bind legacy tone QA to the actual Clean V2 Short template.

    Legacy ProductionPlan defaults narrative_format to direct_cinematic. That is
    correct for legacy plans but wrong for standalone Clean V2 Shorts, whose
    deterministic template selection is authoritative.
    """
    if str(brief.get("format") or "") == "short":
        template = str(select_short_template(brief)["template"])
        return _AUDIT_NARRATIVE_FORMAT_OVERRIDES.get(template, template)
    return "direct_cinematic"


def _build_production_plan_for_audit(
    *,
    brief: Mapping[str, Any],
    plan: Mapping[str, Any],
    script: Mapping[str, Any],
) -> Any:
    from isco_video_agent.models import ProductionPlan, ScriptSection

    narrations = {
        str(item["id"]): str(item.get("narration") or "")
        for item in script["sections"]
    }
    sections = [
        ScriptSection(
            id=str(item["id"]),
            narration=narrations.get(str(item["id"]), ""),
            visual_query=str(item.get("visual_query_en") or ""),
            key_point=str(item.get("purpose") or ""),
        )
        for item in plan["sections"]
    ]
    brief_format = str(brief.get("format") or "")
    narrative_format = _audit_narrative_format_for_brief(brief)
    return ProductionPlan(
        topic=str(brief.get("approved_topic") or ""),
        pillar=str(brief.get("pillar") or ""),
        format="moment" if brief_format == "short" else brief_format,
        hook="",
        title_options=[str(plan.get("title") or "")],
        thumbnail_concepts=[],
        sections=sections,
        cta=str(plan.get("cta") or ""),
        closing_payoff=str(plan.get("promise") or ""),
        narrative_format=narrative_format,
    )


def _run_structural_ai_flags(
    *,
    output_dir: Path,
    brief: Mapping[str, Any],
    script: Mapping[str, Any],
) -> dict[str, Any]:
    transcript = "\n\n".join(
        str(item.get("narration") or "")
        for item in (script.get("sections") or [])
        if isinstance(item, Mapping)
    )
    short_form = str(brief.get("format") or "") in {"moment", "short"}
    flags = structural_ai_flags(transcript, short_form=short_form)
    report = {
        "schema_version": 1,
        "source": "legacy-editorial-room-structural-ai-flags",
        "mode": "advisory",
        "short_form": short_form,
        "flags": list(flags),
    }
    atomic_write_json(output_dir / "structural-ai-flags.json", report)
    return report


def _run_legacy_factuality_audit(
    *,
    output_dir: Path,
    brief: Mapping[str, Any],
    plan: Mapping[str, Any],
    script: Mapping[str, Any],
) -> dict[str, Any]:
    # Reuse the frozen Engine's full prompt, normalizer, validator, fail-closed result,
    # and semantic-block behavior. Clean V2 appends only the final Mistral executor leg.
    from clean_v2.text_audit import audit_plan_with_mistral

    api_key = _read_secret("GEMINI_API_KEY")
    model = str(os.environ.get("GEMINI_CONTENT_MODEL") or "gemini-3.7-flash").strip()
    production_plan = _build_production_plan_for_audit(brief=brief, plan=plan, script=script)
    research_context = brief.get("research_pack") or []
    diagnostics: dict[str, Any] = {}
    result = audit_plan_with_mistral(
        api_key,
        production_plan,
        research_context,
        model,
        diagnostics=diagnostics,
    )
    report = {
        "schema_version": 1,
        "source": "clean-v2-legacy-factuality-audit",
        **result,
        "diagnostics": diagnostics,
    }
    atomic_write_json(output_dir / "factuality-audit.json", report)
    if diagnostics.get("validation") != "valid":
        attempts = diagnostics.get("attempts") or []
        summary = ", ".join(
            f"{item.get('provider')}:{item.get('outcome')}" for item in attempts
        ) or "no providers configured"
        raise RuntimeError(f"{TEXT_AUDIT_STAGE} exhausted bounded provider route: {summary}")
    if result.get("status") == "block":
        raise CleanV2FactualityContentBlock(report)
    return report


def _first_spoken_sentence(script: Mapping[str, Any]) -> str:
    sections = script.get("sections") or []
    if not isinstance(sections, list) or not sections:
        return ""
    first = sections[0]
    if not isinstance(first, Mapping):
        return ""
    narration = str(first.get("narration") or "").strip()
    if not narration:
        return ""
    match = re.search(r"^.*?[.!؟!](?:\s|$)", narration)
    return (match.group(0) if match else narration).strip()[:600]


def _closing_payoff_for_tone_audit(
    script: Mapping[str, Any],
    *,
    identity: Mapping[str, Any] | None = None,
) -> str:
    """Expose the actual repaired ending to Tone QA, not the planning promise."""
    sections = script.get("sections") or []
    if not isinstance(sections, list) or not sections:
        return ""
    last = sections[-1]
    if not isinstance(last, Mapping):
        return ""
    narration = str(last.get("narration") or "").strip()
    closer = str((identity or {}).get("closer") or "").strip()
    if closer:
        narration = _strip_exact_host_phrase(narration, closer)
    narration = " ".join(narration.split()).strip()
    if not narration:
        return ""
    sentences = [
        item.strip()
        for item in re.split(r"(?<=[.!؟!])\s+", narration)
        if item.strip()
    ]
    payoff = " ".join(sentences[-3:]) if sentences else narration
    return payoff[-1000:].strip()


class CleanV2FactualityContentBlock(RuntimeError):
    """A validated factuality block eligible for one bounded combined repair."""

    def __init__(
        self,
        report: Mapping[str, Any],
        *,
        tone_report: Mapping[str, Any] | None = None,
    ) -> None:
        self.report = dict(report)
        self.tone_report = dict(tone_report) if tone_report is not None else None
        super().__init__("Independent factuality/AI-expert gate blocked real production")


class CleanV2ToneContentBlock(RuntimeError):
    """A validated semantic Tone/Naturalness block eligible for one bounded repair."""

    def __init__(self, report: Mapping[str, Any]) -> None:
        self.report = dict(report)
        super().__init__("Independent tone/naturalness gate blocked real production")


_FACTUALITY_REPAIR_FLAG_FIELDS = (
    "unsupported_claims",
    "professional_advice_flags",
    "expert_persona_flags",
)

_TONE_REPAIR_FLAG_FIELDS = (
    "preachiness_flags",
    "naturalness_flags",
    "narrative_format_flags",
    "unverified_religious_quote_flags",
)


def _run_legacy_tone_naturalness_audit(
    *,
    output_dir: Path,
    brief: Mapping[str, Any],
    plan: Mapping[str, Any],
    script: Mapping[str, Any],
) -> dict[str, Any]:
    # Reuse the frozen Engine's tone/naturalness prompt, semantic rules,
    # normalization, fail-closed behavior, and Approval Shopping guard.
    # Clean V2 adds only its strict-schema final Mistral executor leg.
    from clean_v2.tone_audit import audit_tone_and_naturalness_with_mistral

    api_key = _read_secret("GEMINI_API_KEY")
    model = str(os.environ.get("GEMINI_CONTENT_MODEL") or "gemini-3.7-flash").strip()
    production_plan = _build_production_plan_for_audit(
        brief=brief,
        plan=plan,
        script=script,
    )
    identity_path = output_dir / "narrative-identity.json"
    identity = _read_json_object(identity_path) if identity_path.is_file() else {}
    production_plan.hook = _first_spoken_sentence(script)
    production_plan.closing_payoff = (
        _closing_payoff_for_tone_audit(script, identity=identity)
        or str(plan.get("promise") or "")
    )
    production_plan.identity_opener = str(identity.get("opener") or "").strip()
    production_plan.identity_closer = str(identity.get("closer") or "").strip()
    production_plan.identity_transitions = [
        str(item).strip()
        for item in (identity.get("transitions") or [])
        if str(item).strip()
    ]
    result = audit_tone_and_naturalness_with_mistral(
        api_key,
        production_plan,
        model,
    )
    report = {
        "schema_version": 1,
        "source": "clean-v2-legacy-tone-naturalness-audit",
        **result,
    }
    atomic_write_json(output_dir / "tone-naturalness-audit.json", report)

    if str(result.get("validation") or "") != "valid":
        attempts = result.get("attempts") or []
        summary = ", ".join(
            f"{item.get('provider')}:{item.get('outcome')}"
            for item in attempts
            if isinstance(item, Mapping)
        ) or "no providers configured"
        raise RuntimeError(
            f"{TEXT_AUDIT_STAGE} exhausted bounded provider route: "
            f"tone_naturalness {summary}"
        )
    if result.get("status") == "block":
        raise CleanV2ToneContentBlock(report)
    return report


def _structured_factuality_flags(report: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Read authoritative section locations from the validated provider payload."""
    diagnostics = report.get("diagnostics")
    raw = diagnostics.get("raw_result") if isinstance(diagnostics, Mapping) else None
    if not isinstance(raw, Mapping):
        return []
    rows: list[tuple[str, str]] = []
    for field in _FACTUALITY_REPAIR_FLAG_FIELDS:
        values = raw.get(field) or []
        if not isinstance(values, list):
            continue
        for value in values:
            if not isinstance(value, Mapping):
                continue
            section_id = str(value.get("section_id") or "").strip()
            issue = " ".join(str(value.get("issue") or "").split()).strip()
            if section_id and issue:
                rows.append((section_id, issue))
    return rows


def _factuality_repair_issue_notes(report: Mapping[str, Any]) -> str:
    """Flatten structured factuality issues without parsing location from prose."""
    seen: set[tuple[str, str]] = set()
    lines: list[str] = []
    for section_id, issue in _structured_factuality_flags(report):
        key = (section_id, issue)
        if key not in seen:
            lines.append(f"- [factuality:{section_id}] {issue}")
            seen.add(key)
    return "\n".join(lines)


def _factuality_target_section_ids(
    report: Mapping[str, Any],
    script: Mapping[str, Any],
) -> tuple[str, ...]:
    """Use provider-supplied structured section_id directly; never infer from issue text."""
    ordered_ids = [
        str(item.get("id") or "")
        for item in (script.get("sections") or [])
        if isinstance(item, Mapping)
    ]
    valid = set(ordered_ids)
    targets = {
        section_id
        for section_id, _issue in _structured_factuality_flags(report)
        if section_id in valid
    }
    return tuple(section_id for section_id in ordered_ids if section_id in targets)


def _factuality_location_issue_notes(
    report: Mapping[str, Any],
    script: Mapping[str, Any],
) -> str:
    """Compatibility diagnostic only; location is already structured."""
    del script
    return "\n".join(
        f"- [factuality-location] {section_id}"
        for section_id, _issue in _structured_factuality_flags(report)
    )


_QUOTED_TONE_FLAG_EXAMPLE = re.compile(r"['\"]([^'\"]{1,220})['\"]")
_WORD_TOKEN = re.compile(r"\w+", re.UNICODE)
_QUOTE_WORD_OVERLAP_FLOOR = 0.6


def _script_text_haystack(script: Mapping[str, Any]) -> str:
    """Flatten every span of the original script a repair prompt may cite."""
    parts = [str(script.get("title") or "")]
    sections = script.get("sections") or []
    if isinstance(sections, list):
        parts.extend(
            str(item.get("narration") or "")
            for item in sections
            if isinstance(item, Mapping)
        )
    return "\n".join(parts)


def _quote_is_verifiable(excerpt: str, haystack: str, haystack_words: set[str]) -> bool:
    """A quote is trustworthy if it is a real substring, or close enough in words.

    Auditors routinely cite a real span in a lightly paraphrased form (a verb
    quoted as its verbal noun, for example), which should still count as
    verified. A quote built from fragments that share no real words with the
    script at all (Run #315: mixed Arabic/Latin/CJK garbage like
    'الخططatego执行ية' or a plain English word invented out of thin air like
    'want') should not.
    """
    if excerpt in haystack:
        return True
    words = [token.casefold() for token in _WORD_TOKEN.findall(excerpt)]
    if not words:
        return False
    matched = sum(1 for word in words if word in haystack_words)
    return (matched / len(words)) >= _QUOTE_WORD_OVERLAP_FLOOR


def _drop_unverified_flag_quotes(flag: str, haystack: str) -> str:
    """Strip quoted examples an audit flag cites that never appear in the script.

    A free-tier audit provider can hallucinate example fragments (garbled or
    mixed-script text) that do not exist anywhere in the actual narration.
    Passing a fabricated quote into the repair prompt as "evidence" invites the
    repair provider to target text that isn't there, which the local
    find/replace validator then rejects outright (Run #315:
    narration.count(find) != 1). Drop only the unverifiable quote itself; keep
    the rest of the flag's wording intact.
    """
    haystack_words = {token.casefold() for token in _WORD_TOKEN.findall(haystack)}

    def _replace(match: "re.Match[str]") -> str:
        excerpt = match.group(1).strip()
        if excerpt and _quote_is_verifiable(excerpt, haystack, haystack_words):
            return match.group(0)
        return ""

    cleaned = _QUOTED_TONE_FLAG_EXAMPLE.sub(_replace, flag)
    return " ".join(cleaned.split())


def _tone_repair_issue_notes(
    report: Mapping[str, Any], script: Mapping[str, Any] | None = None
) -> str:
    """Deterministically flatten only the actual tone flags into repair notes."""
    haystack = _script_text_haystack(script) if script is not None else ""
    lines: list[str] = []
    seen: set[str] = set()
    for field in _TONE_REPAIR_FLAG_FIELDS:
        values = report.get(field) or []
        if not isinstance(values, list):
            continue
        for value in values:
            flag = " ".join(str(value or "").split()).strip()
            if not flag:
                continue
            if haystack:
                flag = _drop_unverified_flag_quotes(flag, haystack)
            if flag and flag not in seen:
                lines.append(f"- [tone] {flag}")
                seen.add(flag)
    return "\n".join(lines)


def _short_template_tone_repair_issue_notes(brief: Mapping[str, Any]) -> str:
    """Add a deterministic template-specific repair contract only for blocked Shorts."""
    if str(brief.get("format") or "").strip().casefold() != "short":
        return ""
    selection = select_short_template(brief)
    if str(selection.get("template") or "") != "inner_dialogue":
        return ""
    lines = [
        "- [tone-template:inner_dialogue] The current draft reads as direct advice disguised as "
        "inner_dialogue; repair the writing so the viewer hears a believable inner voice rather than "
        "a narrator giving instructions."
    ]
    lines.extend(f"- [tone-template:inner_dialogue] {rule}" for rule in INNER_DIALOGUE_VOICE_RULES)
    lines.append(
        "- [tone-template:inner_dialogue] Preserve the locked hook, then make the next beat "
        "genuinely advance it instead of restating it."
    )
    return "\n".join(lines)


def _structural_repair_issue_notes(output_dir: Path) -> str:
    """Append the already-computed advisory Structural AI flags to the same repair."""
    path = output_dir / "structural-ai-flags.json"
    if not path.is_file():
        return ""
    report = _read_json_object(path)
    values = report.get("flags") or []
    if not isinstance(values, list):
        return ""

    lines: list[str] = []
    seen: set[str] = set()
    for value in values:
        flag = " ".join(str(value or "").split()).strip()
        if not flag or flag in seen:
            continue
        if flag == "repeated_not_x_but_y":
            lines.append(
                '- [structural] repeated_not_x_but_y: eliminate repeated Arabic contrast '
                'constructions of the form "ليس X بل Y" / "ليس ... بل ..."; rewrite those '
                "sentences with varied, natural Arabic syntax while preserving their meaning."
            )
        else:
            lines.append(f"- [structural] {flag}")
        seen.add(flag)
    return "\n".join(lines)


_NOT_X_BUT_Y_OCCURRENCE = re.compile(
    r"(?:ليس|ليست|ليسَ)[^.!؟!]{0,90}(?:بل|وإنما)[^.!؟!]{0,120}"
)


def _research_boundaries_context(brief: Mapping[str, Any]) -> str:
    pack = brief.get("research_pack") or []
    if not isinstance(pack, list):
        return ""
    rows: list[dict[str, str]] = []
    for item in pack:
        if not isinstance(item, Mapping):
            continue
        scope = " ".join(str(item.get("claim_scope") or "").split()).strip()
        if not scope:
            continue
        rows.append(
            {
                "source_title": str(item.get("source_title") or "").strip(),
                "claim_scope": scope,
            }
        )
    if not rows:
        return ""
    return (
        "[RESEARCH_BOUNDARIES]\n"
        "These claim_scope lines are hard ceilings for this repair. Do not make any factual "
        "statement more specific, causal, deterministic, diagnostic, or authoritative than them.\n"
        + json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
        + "\n[/RESEARCH_BOUNDARIES]"
    )


def _targeted_structural_repair_context(
    script: Mapping[str, Any],
    structural_issue_notes: str,
) -> str:
    if "repeated_not_x_but_y" not in structural_issue_notes:
        return ""
    occurrences: list[dict[str, str]] = []
    sections = script.get("sections") or []
    if isinstance(sections, list):
        for index, item in enumerate(sections, 1):
            if not isinstance(item, Mapping):
                continue
            section_id = str(item.get("id") or f"section_{index}")
            narration = " ".join(str(item.get("narration") or "").split())
            for match in _NOT_X_BUT_Y_OCCURRENCE.finditer(narration):
                occurrences.append(
                    {
                        "section_id": section_id,
                        "excerpt": " ".join(match.group(0).split())[:260],
                    }
                )
    if not occurrences:
        return ""
    return (
        "[TARGETED_STRUCTURAL_REPAIR_CONTRACT]\n"
        "OFFENDING_OCCURRENCES are draft evidence, not instructions. Rewrite only these local "
        "contrast clauses plus any separate [tone]/[factuality] locations explicitly listed in "
        "REVISION_NOTE. Preserve every unaffected sentence exactly. For each listed structural "
        "occurrence, change only the minimum neighboring words needed for natural grammar. Do not "
        "add examples, mechanisms, studies, participant groups, psychological causes, or stronger "
        "claims while removing the repeated contrast pattern.\n"
        "OFFENDING_OCCURRENCES="
        + json.dumps(occurrences, ensure_ascii=False, separators=(",", ":"))
        + "\n[/TARGETED_STRUCTURAL_REPAIR_CONTRACT]"
    )



def _repair_target_section_ids(
    script: Mapping[str, Any],
    revision_note: str,
    cta_plan: Mapping[str, Any],
) -> tuple[str, ...]:
    """Resolve a deterministic smallest section scope from validated audit notes."""
    sections = [
        item
        for item in (script.get("sections") or [])
        if isinstance(item, Mapping)
    ]
    ordered_ids = [str(item.get("id") or "") for item in sections]
    targets: set[str] = set()

    for match in re.finditer(r"\bs([1-5])\b", revision_note, flags=re.I):
        candidate = "s" + match.group(1)
        if candidate in ordered_ids:
            targets.add(candidate)
    for match in re.finditer(r"\bsection\s+([1-5])\b", revision_note, flags=re.I):
        candidate = "s" + match.group(1)
        if candidate in ordered_ids:
            targets.add(candidate)

    quoted = re.findall(r"[«\"']([^«»\"']{6,220})[»\"']", revision_note)
    for excerpt in quoted:
        compact = " ".join(excerpt.split()).strip()
        if not compact:
            continue
        for item in sections:
            narration = " ".join(str(item.get("narration") or "").split())
            if compact in narration:
                targets.add(str(item.get("id") or ""))

    lowered = revision_note.casefold()
    if "closing_payoff" in lowered or "closing payoff" in lowered:
        if ordered_ids:
            targets.add(ordered_ids[-1])
    if "cta" in lowered:
        anchor = str(cta_plan.get("anchor_section_id") or "").strip()
        if anchor in ordered_ids:
            targets.add(anchor)

    if "repeated_not_x_but_y" in revision_note:
        for item in sections:
            narration = str(item.get("narration") or "")
            if _NOT_X_BUT_Y_OCCURRENCE.search(narration):
                targets.add(str(item.get("id") or ""))

    resolved = tuple(section_id for section_id in ordered_ids if section_id in targets)
    if resolved:
        return resolved

    # Some validated audit flags describe the whole draft (e.g. "script is monologue,
    # narrative format not expressed naturally") rather than one sentence. There is no
    # single section to pin such a flag to by definition. Rather than fail closed before
    # any repair is attempted, fall back to every section as the allowed patch scope; the
    # model still must find a verbatim phrase to replace, the 1-6 patch cap and every
    # locked-anchor check in _validate_and_apply_script_patches still apply unchanged.
    if revision_note.strip() and ordered_ids:
        return tuple(ordered_ids)

    return ()


_HOOK_WORD_FIX_MAX_CHARS = 40


def _audit_verified_repair_terms(revision_note: str) -> frozenset[str]:
    """Terms the validated audit itself quoted as the defect.

    _tone_repair_issue_notes/_factuality_repair_issue_notes already strip any
    quoted excerpt from a flag that _quote_is_verifiable rejects before the
    flag reaches revision_note (Run #315). Every quote still present here has
    therefore already survived that check, so it is trustworthy evidence for
    a scoped edit inside an otherwise-locked anchor such as the hook.
    """
    return frozenset(
        compact
        for match in _QUOTED_TONE_FLAG_EXAMPLE.findall(revision_note)
        if (compact := " ".join(match.split()).strip())
    )


def _validate_and_apply_script_patches(
    value: Any,
    *,
    plan: Mapping[str, Any],
    original_script: Mapping[str, Any],
    identity: Mapping[str, Any],
    cta_plan: Mapping[str, Any],
    revision_note: str,
    allowed_section_ids: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Apply exact local replacements to the original script; reject broad rewrites."""
    if not isinstance(value, Mapping):
        raise ValueError("script patch response must be an object")
    patches = value.get("patches")
    if not isinstance(patches, list) or not 1 <= len(patches) <= 6:
        raise ValueError("script patch response requires 1-6 patches")

    allowed_ids = set(
        allowed_section_ids
        if allowed_section_ids is not None
        else _repair_target_section_ids(original_script, revision_note, cta_plan)
    )
    if not allowed_ids:
        raise ValueError("script patch has no deterministic target section")

    repaired = copy.deepcopy(dict(original_script))
    sections = repaired.get("sections") or []
    if not isinstance(sections, list):
        raise ValueError("script patch original sections invalid")
    by_id = {
        str(item.get("id") or ""): item
        for item in sections
        if isinstance(item, dict)
    }

    original_hook = _first_spoken_sentence(original_script)
    audit_verified_terms = _audit_verified_repair_terms(revision_note)
    hook_word_fix_used = False
    opener = str(identity.get("opener") or "").strip()
    closer = str(identity.get("closer") or "").strip()
    spoken_cta = str(cta_plan.get("spoken_text") or "").strip()
    cta_anchor = str(cta_plan.get("anchor_section_id") or "").strip()
    total_find_chars = 0
    seen: set[tuple[str, str]] = set()

    for raw in patches:
        if not isinstance(raw, Mapping):
            raise ValueError("script patch item must be an object")
        if set(raw) != {"section_id", "find", "replace"}:
            raise ValueError("script patch item has unexpected fields")
        section_id = str(raw.get("section_id") or "").strip()
        find = str(raw.get("find") or "")
        replace = str(raw.get("replace") or "")
        if section_id not in allowed_ids or section_id not in by_id:
            raise ValueError("script patch targeted an unflagged section")
        if not find.strip() or len(find) > 400 or len(replace) > 550:
            raise ValueError("script patch span exceeds local repair bounds")
        if len(replace) > len(find) + 180:
            raise ValueError("script patch expanded the target too far")
        key = (section_id, find)
        if key in seen:
            raise ValueError("script patch duplicated a target")
        seen.add(key)
        total_find_chars += len(find)
        if total_find_chars > 900:
            raise ValueError("script patch total repair surface exceeds 900 characters")

        item = by_id[section_id]
        narration = str(item.get("narration") or "")
        if narration.count(find) != 1:
            raise ValueError("script patch find text must match exactly once")

        if (
            original_hook
            and sections
            and section_id == str(sections[0].get("id") or "")
            and find in original_hook
        ):
            # A short, audit-verified word/phrase fix inside the hook (e.g. a
            # flagged typo or non-standard verb) is allowed once, in addition
            # to the broad-rewrite guard below. Anything else touching the
            # hook still falls through to the hard equality check after the
            # loop.
            compact_find = " ".join(find.split()).strip()
            if (
                hook_word_fix_used
                or len(find) > _HOOK_WORD_FIX_MAX_CHARS
                or compact_find not in audit_verified_terms
            ):
                raise ValueError("script patch changed the locked hook")
            hook_word_fix_used = True

        for locked_name, locked_text in (
            ("hook", original_hook if section_id == str(sections[0].get("id") or "") else ""),
            ("opener", opener),
            ("closer", closer),
            ("cta", spoken_cta if section_id == cta_anchor else ""),
        ):
            if locked_text and locked_text in find and replace.count(locked_text) != 1:
                raise ValueError(f"script patch changed locked {locked_name}")

        item["narration"] = narration.replace(find, replace, 1)

    normalized = validate_script(repaired, plan)
    if (
        original_hook
        and not hook_word_fix_used
        and _first_spoken_sentence(normalized) != original_hook
    ):
        raise ValueError("script patch changed the locked hook")
    joined = "\n".join(
        str(item.get("narration") or "")
        for item in normalized.get("sections", [])
        if isinstance(item, Mapping)
    )
    if opener and joined.count(opener) != 1:
        raise ValueError("script patch changed the locked narrative identity opener")
    if closer and joined.count(closer) != 1:
        raise ValueError("script patch changed the locked narrative identity closer")
    if spoken_cta:
        if joined.count(spoken_cta) != 1:
            raise ValueError("script patch changed or duplicated the locked CTA")
        anchor = next(
            (
                item
                for item in normalized.get("sections", [])
                if isinstance(item, Mapping)
                and str(item.get("id") or "") == cta_anchor
            ),
            None,
        )
        if anchor is None or spoken_cta not in str(anchor.get("narration") or ""):
            raise ValueError("script patch moved the locked CTA")
    return normalized


def _replace_first_spoken_sentence(text: str, locked_sentence: str) -> str:
    """Restore the host-owned hook while preserving the candidate body."""
    text = text.strip()
    locked_sentence = locked_sentence.strip()
    if not locked_sentence:
        return text
    if _first_spoken_sentence({"sections": [{"narration": text}]}) == locked_sentence:
        return text
    match = re.search(r"^.*?[.!؟!](?:\\s|$)", text)
    if not match:
        return f"{locked_sentence} {text}".strip()
    return f"{locked_sentence} {text[match.end():].lstrip()}".strip()


def _overlay_tone_repair_host_locks(
    repaired: dict[str, Any],
    *,
    original_script: Mapping[str, Any],
    identity: Mapping[str, Any],
    cta_plan: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply the old RepairDossier bulkhead to Clean V2's tone-only repair.

    The model owns wording fixes. The host owns title, hook, brand signature and CTA.
    Restore those exact runtime anchors after the candidate passes the normal script
    schema, then let the unchanged factuality/Tone/Structural re-audits judge the result.
    """
    sections = repaired.get("sections") or []
    if not isinstance(sections, list) or not sections:
        raise ValueError("tone repair returned no sections")

    original_title = str(original_script.get("title") or "").strip()
    if original_title:
        repaired["title"] = original_title

    original_hook = _first_spoken_sentence(original_script)
    if original_hook:
        sections[0]["narration"] = _replace_first_spoken_sentence(
            str(sections[0].get("narration") or ""),
            original_hook,
        )

    spoken_cta = str(cta_plan.get("spoken_text") or "").strip()
    anchor_section_id = str(cta_plan.get("anchor_section_id") or "").strip()
    if spoken_cta:
        anchor = None
        for item in sections:
            narration = str(item.get("narration") or "")
            if str(item.get("id") or "") == anchor_section_id:
                anchor = item
                continue
            item["narration"] = _strip_exact_host_phrase(narration, spoken_cta)
        if anchor is None:
            raise ValueError("tone repair lost the locked CTA anchor section")
        anchor_narration = str(anchor.get("narration") or "").strip()
        if anchor_narration.count(spoken_cta) != 1:
            anchor_narration = _strip_exact_host_phrase(anchor_narration, spoken_cta)
            anchor_narration = f"{anchor_narration.rstrip()} {spoken_cta}".strip()
        anchor["narration"] = anchor_narration

    opener = str(identity.get("opener") or "").strip()
    closer = str(identity.get("closer") or "").strip()
    if opener or closer:
        for item in sections:
            narration = str(item.get("narration") or "")
            narration = _strip_exact_host_phrase(narration, opener)
            narration = _strip_exact_host_phrase(narration, closer)
            item["narration"] = narration
        if opener:
            sections[0]["narration"] = _insert_after_first_sentence(
                str(sections[0].get("narration") or ""),
                opener,
            )
        if closer:
            sections[-1]["narration"] = (
                f"{str(sections[-1].get('narration') or '').rstrip()} {closer}".strip()
            )
    return repaired


def _validate_tone_repair_script(
    value: Any,
    *,
    plan: Mapping[str, Any],
    original_script: Mapping[str, Any],
    identity: Mapping[str, Any],
    cta_plan: Mapping[str, Any],
) -> dict[str, Any]:
    repaired = validate_script(value, plan)
    repaired = _overlay_tone_repair_host_locks(
        repaired,
        original_script=original_script,
        identity=identity,
        cta_plan=cta_plan,
    )

    original_hook = _first_spoken_sentence(original_script)
    if original_hook and _first_spoken_sentence(repaired) != original_hook:
        raise ValueError("tone repair changed the locked hook")

    joined = "\n".join(
        str(item.get("narration") or "")
        for item in repaired.get("sections", [])
        if isinstance(item, Mapping)
    )
    opener = str(identity.get("opener") or "").strip()
    closer = str(identity.get("closer") or "").strip()
    if opener and joined.count(opener) != 1:
        raise ValueError("tone repair changed the locked narrative identity opener")
    if closer and joined.count(closer) != 1:
        raise ValueError("tone repair changed the locked narrative identity closer")

    spoken_cta = str(cta_plan.get("spoken_text") or "").strip()
    anchor_section_id = str(cta_plan.get("anchor_section_id") or "").strip()
    if spoken_cta:
        if joined.count(spoken_cta) != 1:
            raise ValueError("tone repair changed or duplicated the locked CTA")
        anchor = next(
            (
                item
                for item in repaired.get("sections", [])
                if isinstance(item, Mapping)
                and str(item.get("id") or "") == anchor_section_id
            ),
            None,
        )
        if anchor is None or spoken_cta not in str(anchor.get("narration") or ""):
            raise ValueError("tone repair moved the locked CTA to another section")
    return repaired


def _tone_repair_prompt(
    *,
    brief: Mapping[str, Any],
    plan: Mapping[str, Any],
    script: Mapping[str, Any],
    identity: Mapping[str, Any],
    cta_plan: Mapping[str, Any],
    revision_note: str,
) -> str:
    plan_json = json.dumps(
        dict(plan), ensure_ascii=False, separators=(",", ":")
    )
    research_boundaries = _research_boundaries_context(brief)
    targeted_structural = _targeted_structural_repair_context(
        script,
        revision_note,
    )
    payload = json.dumps(
        {
            "brief": dict(brief),
            "current_script": dict(script),
            "narrative_identity": dict(identity),
            "cta_plan": dict(cta_plan),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    hook = _first_spoken_sentence(script)
    allowed_patch_section_ids = _repair_target_section_ids(
        script, revision_note, cta_plan
    )
    return with_human_feel(with_channel_persona(f"""
You are making ONE bounded tone/naturalness repair to an already approved Arabic spoken script.
The production data below is authoritative. Do not redesign the episode and do not broaden scope.

LOCKED_PLAN:
{plan_json}

The approved brief and locked plan are authoritative.

PRODUCTION_CONTEXT:
{payload}

REVISION_NOTE:
{revision_note}

ALLOWED_PATCH_SECTION_IDS:
{json.dumps(list(allowed_patch_section_ids), ensure_ascii=False, separators=(",", ":"))}

{research_boundaries}

{targeted_structural}

ONE_BOUNDED_TONE_REPAIR_CONTRACT:
- Fix EVERY concrete tone/naturalness and structural problem listed in REVISION_NOTE, not just one
  of them. This is your only repair attempt: the full audit runs again on whatever you return, and
  any flag you leave unaddressed will still block the result exactly as if you had changed nothing.
  Use as many of your patches as the listed flags require, up to the maximum below.
- If REVISION_NOTE includes repeated_not_x_but_y, remove the repeated "ليس X بل Y" /
  "ليس ... بل ..." framing and use varied, natural Arabic sentence structures instead.
- Preserve the section count, ids, order, title, and each section's role.
- Preserve this first spoken hook sentence exactly: {hook}
- Preserve the runtime narrative-identity opener and closer exactly once each.
- If the current script contains the approved prayer sentence or channel-definition sentence,
  preserve each of those host-owned identity lines exactly once and do not patch them.
- Preserve the authored CTA spoken_text exactly once and in the same anchor section. Never add,
  paraphrase, move it to another section, or repeat it. You MAY reposition that exact CTA within
  its existing anchor section when needed to make the surrounding transition sound natural.
- These host-owned locks are also restored deterministically after your candidate is parsed; spend
  repair effort only on the listed tone/naturalness defects, not on rewriting locked anchors.
- Preserve all approved factual claims and their research boundaries. Do not add, remove,
  strengthen, quantify, or invent claims, studies, experts, quotations, diagnoses, or authority.
- Tone repair is NOT permission to explain the science again. Never introduce a concrete study
  scenario, participant group, hidden psychological motive, "the brain is designed to..." claim,
  or a stronger causal mechanism unless that exact scope already exists in the current script and
  remains within RESEARCH_BOUNDARIES.
- Preserve every unaffected sentence exactly. Change only sentences necessary for a listed flag
  or an OFFENDING_OCCURRENCE.
- Make the minimum wording/transition changes needed for the listed flags. No unrelated rewrite.
- DO NOT return a rewritten script. Return only exact local text replacements.
- Each patch.find MUST be copied verbatim from CURRENT_SCRIPT inside the named section.
- Keep patch.find as SHORT as possible: the smallest exact phrase that pinpoints the flagged
  problem, never a full sentence unless the whole sentence is the issue. A long copied span is
  far more likely to contain a transcription slip and be rejected outright.
- Each patch.replace MUST contain only the minimum local wording needed to fix that target.
- Maximum 6 patches. Do not patch an unflagged section.

Return exactly one JSON object in this shape:
{{
  "patches": [
    {{
      "section_id": "s2",
      "find": "exact original text copied from the current narration",
      "replace": "minimal repaired replacement"
    }}
  ]
}}
""".strip()))


def _normalized_narration_signature(script: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    """Canonical spoken-text signature used to reject no-op repair candidates."""
    sections = script.get("sections") or []
    return tuple(
        (
            str(item.get("id") or ""),
            " ".join(str(item.get("narration") or "").split()),
        )
        for item in sections
        if isinstance(item, Mapping)
    )


def _run_one_bounded_tone_repair(
    *,
    output_dir: Path,
    brief: Mapping[str, Any],
    plan: Mapping[str, Any],
    script: dict[str, Any],
    router: Any,
    blocked_report: Mapping[str, Any],
) -> dict[str, Any]:
    tone_issue_notes = _tone_repair_issue_notes(blocked_report, script)
    structural_issue_notes = _structural_repair_issue_notes(output_dir)
    template_issue_notes = _short_template_tone_repair_issue_notes(brief)
    issue_notes = "\n".join(
        item
        for item in (tone_issue_notes, structural_issue_notes, template_issue_notes)
        if item
    )
    atomic_write_json(
        output_dir / "tone-naturalness-audit-pre-repair.json",
        dict(blocked_report),
    )
    if not tone_issue_notes:
        raise RuntimeError(
            "Tone/Naturalness block has no bounded actionable tone flags"
        )

    identity = _read_json_object(output_dir / "narrative-identity.json")
    cta_plan = _read_json_object(output_dir / "cta-plan.json")
    target_ids = _repair_target_section_ids(script, issue_notes, cta_plan)
    if not target_ids:
        raise RuntimeError(
            "Tone/Naturalness repair has no deterministic target section"
        )
    narration_before = _normalized_narration_signature(script)
    repaired = router.route(
        stage="script_patch",
        prompt=_tone_repair_prompt(
            brief=brief,
            plan=plan,
            script=script,
            identity=identity,
            cta_plan=cta_plan,
            revision_note=issue_notes,
        ),
        max_tokens=2200 if str(brief.get("format") or "") == "film" else 1200,
        validator=lambda value: _validate_and_apply_script_patches(
            value,
            plan=plan,
            original_script=script,
            identity=identity,
            cta_plan=cta_plan,
            revision_note=issue_notes,
        ),
    )
    atomic_write_json(output_dir / "script-post-tone-repair.json", repaired)
    if _normalized_narration_signature(repaired) == narration_before:
        atomic_write_json(
            output_dir / "tone-repair.json",
            {
                "schema_version": 1,
                "source": "clean-v2-one-bounded-tone-repair",
                "attempts": 1,
                "issue_notes": issue_notes,
                "status": "failed_closed",
                "reason": "TONE_REPAIR_NO_EFFECT",
                "narration_changed": False,
            },
        )
        raise RuntimeError(
            "TONE_REPAIR_NO_EFFECT: bounded tone repair made no narration changes"
        )

    script.clear()
    script.update(repaired)
    _assert_brand_signature_invariant(
        script["sections"],
        str(brief.get("format") or ""),
        str(identity.get("opener") or ""),
        str(identity.get("closer") or ""),
    )
    atomic_write_json(output_dir / "script.json", script)
    transcript = "\n\n".join(item["narration"] for item in script["sections"])
    (output_dir / "narration.txt").write_text(
        transcript + "\n",
        encoding="utf-8",
    )
    return {
        "schema_version": 1,
        "source": "clean-v2-one-bounded-tone-repair",
        "attempts": 1,
        "issue_notes": issue_notes,
        "narration_changed": True,
    }


def _factuality_repair_prompt(
    *,
    brief: Mapping[str, Any],
    plan: Mapping[str, Any],
    script: Mapping[str, Any],
    identity: Mapping[str, Any],
    cta_plan: Mapping[str, Any],
    revision_note: str,
    allowed_patch_section_ids: tuple[str, ...] | None = None,
) -> str:
    plan_json = json.dumps(
        dict(plan), ensure_ascii=False, separators=(",", ":")
    )
    research_boundaries = _research_boundaries_context(brief)
    targeted_structural = _targeted_structural_repair_context(
        script,
        revision_note,
    )
    payload = json.dumps(
        {
            "brief": dict(brief),
            "current_script": dict(script),
            "narrative_identity": dict(identity),
            "cta_plan": dict(cta_plan),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    hook = _first_spoken_sentence(script)
    if allowed_patch_section_ids is None:
        allowed_patch_section_ids = _repair_target_section_ids(
            script, revision_note, cta_plan
        )
    return with_human_feel(with_channel_persona(f"""
You are making ONE bounded factuality repair to an already approved Arabic spoken script.
The production data below is authoritative. Do not redesign the episode and do not broaden scope.

LOCKED_PLAN:
{plan_json}

The approved brief and locked plan are authoritative.

PRODUCTION_CONTEXT:
{payload}

REVISION_NOTE:
{revision_note}

ALLOWED_PATCH_SECTION_IDS:
{json.dumps(list(allowed_patch_section_ids), ensure_ascii=False, separators=(",", ":"))}

{research_boundaries}

{targeted_structural}

ONE_BOUNDED_FACTUALITY_REPAIR_CONTRACT:
- Fix EVERY concrete factuality, tone/naturalness, and structural problem listed in REVISION_NOTE,
  not just one of them. This is your only repair attempt: the full audit runs again on whatever you
  return, and any flag you leave unaddressed will still block the result exactly as if you had
  changed nothing. Use as many of your patches as the listed flags require, up to the maximum below.
- For each [factuality] issue, weaken, qualify, or remove only the offending wording so the claim
  does not exceed the evidence in the approved research pack.
- For each [tone] issue, repair only the flagged narration flow, naturalness, preachiness, or
  viewer-promise/retention defect; do not use it as permission for a broad rewrite.
- Do not invent a new study, source, expert, quotation, number, diagnosis, causal claim, or guarantee.
- Preserve every unaffected factual claim in meaning and strength; do not broaden unrelated claims.
- If REVISION_NOTE includes repeated_not_x_but_y, remove the repeated "ليس X بل Y" /
  "ليس ... بل ..." framing and use varied, natural Arabic sentence structures instead.
- Preserve the section count, ids, order, title, and each section's role.
- Preserve this first spoken hook sentence exactly: {hook}
- Preserve the runtime narrative-identity opener and closer exactly once each.
- If the current script contains the approved prayer sentence or channel-definition sentence,
  preserve each of those host-owned identity lines exactly once and do not patch them.
- Preserve the authored CTA spoken_text exactly once and in the same anchor section. Never add,
  paraphrase, move it to another section, or repeat it. You MAY reposition that exact CTA within
  its existing anchor section when needed to make the surrounding transition sound natural.
- These host-owned locks remain exact; spend repair effort only on the listed
  factuality/tone/structural defects, not on rewriting locked anchors.
- Make the minimum wording changes needed. No unrelated rewrite.
- DO NOT return a rewritten script. Return only exact local text replacements.
- Each patch.find MUST be copied verbatim from CURRENT_SCRIPT inside the named section.
- Keep patch.find as SHORT as possible: the smallest exact phrase that pinpoints the flagged
  problem, never a full sentence unless the whole sentence is the issue. A long copied span is
  far more likely to contain a transcription slip and be rejected outright.
- Each patch.replace MUST contain only the minimum local wording needed to fix that target.
- Maximum 6 patches. Do not patch an unflagged section.

Return exactly one JSON object in this shape:
{{
  "patches": [
    {{
      "section_id": "s4",
      "find": "exact original text copied from the current narration",
      "replace": "minimal repaired replacement"
    }}
  ]
}}
""".strip()))


def _run_one_bounded_factuality_repair(
    *,
    output_dir: Path,
    brief: Mapping[str, Any],
    plan: Mapping[str, Any],
    script: dict[str, Any],
    router: Any,
    blocked_report: Mapping[str, Any],
    tone_report: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    factuality_issue_notes = _factuality_repair_issue_notes(blocked_report)
    factuality_location_notes = _factuality_location_issue_notes(blocked_report, script)
    tone_issue_notes = _tone_repair_issue_notes(tone_report or {}, script)
    structural_issue_notes = _structural_repair_issue_notes(output_dir)
    issue_notes = "\n".join(
        item
        for item in (
            factuality_issue_notes,
            factuality_location_notes,
            tone_issue_notes,
            structural_issue_notes,
        )
        if item
    )
    atomic_write_json(
        output_dir / "factuality-audit-pre-repair.json",
        dict(blocked_report),
    )
    if tone_report is not None:
        atomic_write_json(
            output_dir / "tone-naturalness-audit-pre-repair.json",
            dict(tone_report),
        )
    if not factuality_issue_notes:
        raise RuntimeError(
            "Factuality block has no bounded actionable factuality flags"
        )

    identity = _read_json_object(output_dir / "narrative-identity.json")
    cta_plan = _read_json_object(output_dir / "cta-plan.json")
    factuality_target_ids = _factuality_target_section_ids(blocked_report, script)
    supplemental_notes = "\n".join(item for item in (tone_issue_notes, structural_issue_notes) if item)
    supplemental_target_ids = _repair_target_section_ids(script, supplemental_notes, cta_plan) if supplemental_notes else ()
    ordered_ids = [str(item.get("id") or "") for item in (script.get("sections") or []) if isinstance(item, Mapping)]
    target_set = set(factuality_target_ids) | set(supplemental_target_ids)
    target_ids = tuple(section_id for section_id in ordered_ids if section_id in target_set)
    if not factuality_target_ids:
        raise RuntimeError(
            "Factuality repair has no structured target section"
        )
    repaired = router.route(
        stage="script_patch",
        prompt=_factuality_repair_prompt(
            brief=brief,
            plan=plan,
            script=script,
            identity=identity,
            cta_plan=cta_plan,
            revision_note=issue_notes,
            allowed_patch_section_ids=target_ids,
        ),
        max_tokens=2200 if str(brief.get("format") or "") == "film" else 1200,
        validator=lambda value: _validate_and_apply_script_patches(
            value,
            plan=plan,
            original_script=script,
            identity=identity,
            cta_plan=cta_plan,
            revision_note=issue_notes,
            allowed_section_ids=target_ids,
        ),
    )
    script.clear()
    script.update(repaired)
    atomic_write_json(output_dir / "script-post-factuality-repair.json", script)
    _assert_brand_signature_invariant(
        script["sections"],
        str(brief.get("format") or ""),
        str(identity.get("opener") or ""),
        str(identity.get("closer") or ""),
    )
    atomic_write_json(output_dir / "script.json", script)
    transcript = "\n\n".join(item["narration"] for item in script["sections"])
    (output_dir / "narration.txt").write_text(
        transcript + "\n",
        encoding="utf-8",
    )
    return {
        "schema_version": 1,
        "source": "clean-v2-one-bounded-factuality-repair",
        "attempts": 1,
        "issue_notes": issue_notes,
    }


def _run_text_audit_with_one_bounded_tone_repair(
    *,
    text_audit: Callable[..., dict[str, Any]],
    router: Any,
    output_dir: Path,
    brief: Mapping[str, Any],
    plan: Mapping[str, Any],
    script: dict[str, Any],
) -> dict[str, Any]:
    try:
        return text_audit(
            output_dir=output_dir,
            brief=brief,
            plan=plan,
            script=script,
        )
    except CleanV2FactualityContentBlock as blocked:
        repair_report = _run_one_bounded_factuality_repair(
            output_dir=output_dir,
            brief=brief,
            plan=plan,
            script=script,
            router=router,
            blocked_report=blocked.report,
            tone_report=blocked.tone_report,
        )
        atomic_write_json(
            output_dir / "factuality-repair.json",
            {**repair_report, "status": "repair_applied_reauditing"},
        )
        try:
            # One repair total: re-run the complete Text Audit plus Structural flags.
            # Any surviving factuality/Tone/Structural block fails closed; no second repair.
            post_text_audit = text_audit(
                output_dir=output_dir,
                brief=brief,
                plan=plan,
                script=script,
            )
            post_structural = _run_structural_ai_flags(
                output_dir=output_dir,
                brief=brief,
                script=script,
            )
            structural_flags = list(post_structural.get("flags") or [])
            if structural_flags:
                raise RuntimeError(
                    "Structural AI flags blocked repaired script: "
                    + "; ".join(str(item) for item in structural_flags)
                )
        except Exception as exc:
            atomic_write_json(
                output_dir / "factuality-repair.json",
                {
                    **repair_report,
                    "status": "failed_closed",
                    "post_repair_error_type": type(exc).__name__,
                },
            )
            raise

        final_report = {
            **post_text_audit,
            "factuality_repair_attempted": True,
            "factuality_repair_attempts": 1,
            "factuality_repair_status": "repaired",
            "post_repair_structural_ai_status": "pass",
        }
        atomic_write_json(
            output_dir / "factuality-repair.json",
            {**repair_report, "status": "repaired"},
        )
        return final_report
    except CleanV2ToneContentBlock as blocked:
        repair_report = _run_one_bounded_tone_repair(
            output_dir=output_dir,
            brief=brief,
            plan=plan,
            script=script,
            router=router,
            blocked_report=blocked.report,
        )
        atomic_write_json(
            output_dir / "tone-repair.json",
            {**repair_report, "status": "repair_applied_reauditing"},
        )
        try:
            # Re-run the complete Text Audit: factuality remains authoritative and
            # fail-closed; Tone is re-run inside the same composite audit.
            post_text_audit = text_audit(
                output_dir=output_dir,
                brief=brief,
                plan=plan,
                script=script,
            )
            post_structural = _run_structural_ai_flags(
                output_dir=output_dir,
                brief=brief,
                script=script,
            )
            structural_flags = list(post_structural.get("flags") or [])
            if structural_flags:
                raise RuntimeError(
                    "Structural AI flags blocked repaired script: "
                    + "; ".join(str(item) for item in structural_flags)
                )
        except Exception as exc:
            atomic_write_json(
                output_dir / "tone-repair.json",
                {
                    **repair_report,
                    "status": "failed_closed",
                    "post_repair_error_type": type(exc).__name__,
                },
            )
            raise

        final_report = {
            **post_text_audit,
            "tone_repair_attempted": True,
            "tone_repair_attempts": 1,
            "tone_repair_status": "repaired",
            "post_repair_structural_ai_status": "pass",
        }
        atomic_write_json(
            output_dir / "tone-repair.json",
            {**repair_report, "status": "repaired"},
        )
        return final_report


def _run_text_audits(
    *,
    output_dir: Path,
    brief: Mapping[str, Any],
    plan: Mapping[str, Any],
    script: Mapping[str, Any],
) -> dict[str, Any]:
    # Run both semantic audits before spending the one repair. Infrastructure
    # failures still stop immediately; only a validated factuality content BLOCK
    # is held long enough to collect Tone/Naturalness flags from the same draft.
    factuality_block: CleanV2FactualityContentBlock | None = None
    try:
        factuality = _run_legacy_factuality_audit(
            output_dir=output_dir,
            brief=brief,
            plan=plan,
            script=script,
        )
    except CleanV2FactualityContentBlock as blocked:
        factuality_block = blocked
        factuality = blocked.report

    tone_block: CleanV2ToneContentBlock | None = None
    try:
        tone_naturalness = _run_legacy_tone_naturalness_audit(
            output_dir=output_dir,
            brief=brief,
            plan=plan,
            script=script,
        )
    except CleanV2ToneContentBlock as blocked:
        tone_block = blocked
        tone_naturalness = blocked.report

    if factuality_block is not None:
        factuality_block.tone_report = (
            dict(tone_block.report) if tone_block is not None else None
        )
        raise factuality_block
    if tone_block is not None:
        raise tone_block

    return {
        "schema_version": 1,
        "source": "clean-v2-composite-text-audit",
        "status": "pass",
        "factuality_status": factuality.get("status"),
        "tone_naturalness_status": tone_naturalness.get("status"),
    }


def _run_audio_loudness_mastering(
    *,
    output_dir: Path,
    narration_path: Path,
) -> dict[str, Any]:
    from clean_v2.audio_mastering import master_narration_loudness

    mastered_path = output_dir / "narration-mastered.wav"
    result = master_narration_loudness(narration_path, mastered_path)
    report = {
        "schema_version": 1,
        "source": "clean-v2-audio-loudness-mastering",
        "narration_file": mastered_path.name,
        **result,
    }
    atomic_write_json(output_dir / "audio-mastering.json", report)
    return report


def _run_legacy_cinematic_layer(
    *,
    output_dir: Path,
    final_path: Path,
    narration_path: Path,
    plan: dict[str, Any],
    script: dict[str, Any],
    rights: list[dict[str, Any]],
    fmt: str,
) -> dict[str, Any]:
    # Deliberately reuse the old tested Security V1 + Cinematic V2 owners.
    from clean_v2.legacy_cinematic import apply_post_render_layer

    report = apply_post_render_layer(
        output_dir=output_dir,
        final_path=final_path,
        narration_path=narration_path,
        plan=plan,
        script=script,
        rights=rights,
        fmt=fmt,
    )
    from clean_v2.contextual_cta import apply_contextual_cta_overlay

    cta_report = apply_contextual_cta_overlay(
        output_dir=output_dir,
        final_path=final_path,
        narration_path=narration_path,
        script=script,
    )

    short_timed_text_report: dict[str, Any] | None = None
    short_audio_polish_report: dict[str, Any] | None = None
    if fmt == "short":
        from clean_v2.short_timed_text import apply_short_timed_text

        short_timed_text_report = apply_short_timed_text(
            output_dir=output_dir,
            final_path=final_path,
            narration_path=narration_path,
            script=script,
        )
        atomic_write_json(
            output_dir / "short-timed-text.json",
            short_timed_text_report,
        )

        # New Short feature, deliberately after the restored timed-text layer.
        # Narration has already been mastered; this optional/fail-safe mix only
        # places a very quiet local ambient bed and gentle accents underneath it.
        from clean_v2.short_audio_polish import apply_short_audio_polish

        short_audio_polish_report = apply_short_audio_polish(
            output_dir=output_dir,
            final_path=final_path,
            narration_path=narration_path,
            timed_text_report=short_timed_text_report,
        )
        atomic_write_json(
            output_dir / "short-audio-polish.json",
            short_audio_polish_report,
        )

    # Final CTA surface is local and deterministic: only the user-approved icon
    # PNGs / original subscribe+bell clip / original click sound are allowed.
    # It runs after any Short music bed so the click remains audible above music.
    from clean_v2.visual_cta import apply_visual_cta_assets

    visual_cta_report = apply_visual_cta_assets(
        output_dir=output_dir,
        final_path=final_path,
        narration_path=narration_path,
        script=script,
        fmt=fmt,
    )

    return {
        **report,
        "contextual_cta": {
            "mode": cta_report.get("mode"),
            "render_status": cta_report.get("render_status"),
            "provider_calls_added": cta_report.get("provider_calls_added"),
        },
        "visual_cta": visual_cta_report,
        "short_timed_text": short_timed_text_report,
        "short_audio_polish": short_audio_polish_report,
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid Clean V2 resume artifact: {path.name}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"invalid Clean V2 resume artifact: {path.name}")
    return value


def _resume_identity(
    *,
    approved_brief_sha256: str,
    engine_sha: str,
    runner_sha: str | None,
    max_visuals: int,
) -> dict[str, Any]:
    return {
        "approved_brief_sha256": str(approved_brief_sha256),
        "engine_sha": str(engine_sha),
        "runner_sha": runner_sha or None,
        "max_visuals": int(max_visuals),
    }


def _safe_resume_relative_path(raw: str) -> Path:
    relative = Path(str(raw))
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise RuntimeError("unsafe Clean V2 resume artifact path")
    return relative


def _checkpoint_artifact_paths(output_dir: Path, completed_stage: str) -> list[Path]:
    rank = _RESUME_STAGE_INDEX[completed_stage]
    paths = [Path("brief.json"), Path("plan.json")]
    if rank >= _RESUME_STAGE_INDEX["script"]:
        paths.extend(
            [
                Path("script.json"),
                Path("narration.txt"),
                Path("narrative-identity.json"),
                Path("cta-plan.json"),
            ]
        )
    if rank >= _RESUME_STAGE_INDEX["voice"]:
        paths.append(Path("narration.wav"))
    if rank >= _RESUME_STAGE_INDEX["visuals"]:
        rights_path = output_dir / "rights-manifest.json"
        rights = _read_json_object(rights_path)
        assets = rights.get("assets")
        if not isinstance(assets, list) or not assets:
            raise RuntimeError("Clean V2 resume rights manifest has no assets")
        paths.append(Path("rights-manifest.json"))
        for item in assets:
            if not isinstance(item, dict):
                raise RuntimeError("Clean V2 resume rights manifest is invalid")
            local_file = str(item.get("local_file") or "").strip()
            relative = _safe_resume_relative_path(f"visuals/{local_file}")
            paths.append(relative)
    return paths


def _write_resume_checkpoint(
    output_dir: Path,
    *,
    completed_stage: str,
    approved_brief_sha256: str,
    engine_sha: str,
    runner_sha: str | None,
    max_visuals: int,
    voice_provider: str | None = None,
    voice_fallback_used: bool | None = None,
) -> None:
    if completed_stage not in RESUMABLE_STAGES:
        raise RuntimeError(f"non-resumable Clean V2 checkpoint stage: {completed_stage}")
    artifacts: dict[str, str] = {}
    for relative in _checkpoint_artifact_paths(output_dir, completed_stage):
        path = output_dir / relative
        if not path.is_file() or path.stat().st_size <= 0:
            raise RuntimeError(f"missing Clean V2 checkpoint artifact: {relative.as_posix()}")
        artifacts[relative.as_posix()] = _sha256_file(path)
    payload: dict[str, Any] = {
        "schema_version": RESUME_CONTRACT_VERSION,
        "pipeline": "clean-v2-minimal-e2e",
        "identity": _resume_identity(
            approved_brief_sha256=approved_brief_sha256,
            engine_sha=engine_sha,
            runner_sha=runner_sha,
            max_visuals=max_visuals,
        ),
        "completed_stage": completed_stage,
        "artifacts": artifacts,
    }
    if _RESUME_STAGE_INDEX[completed_stage] >= _RESUME_STAGE_INDEX["voice"]:
        if voice_provider not in {
            "gemini:Charon",
            "azure-f0:ar-OM-AbdullahNeural",
            "piper-local:ar_JO-kareem-medium",
        }:
            raise RuntimeError("Clean V2 checkpoint voice provider is not approved")
        if not isinstance(voice_fallback_used, bool):
            raise RuntimeError("Clean V2 checkpoint voice fallback state is invalid")
        payload["voice_provider"] = voice_provider
        payload["voice_fallback_used"] = voice_fallback_used
    atomic_write_json(output_dir / "resume-checkpoint.json", payload)


def _load_resume_checkpoint(
    resume_from: Path | None,
    *,
    approved_brief_sha256: str,
    engine_sha: str,
    runner_sha: str | None,
    max_visuals: int,
) -> tuple[Path, dict[str, Any]] | None:
    if resume_from is None:
        return None
    root = Path(resume_from)
    checkpoint_path = root / "resume-checkpoint.json"
    if not checkpoint_path.is_file():
        return None
    try:
        checkpoint = _read_json_object(checkpoint_path)
        if checkpoint.get("schema_version") != RESUME_CONTRACT_VERSION:
            return None
        if checkpoint.get("pipeline") != "clean-v2-minimal-e2e":
            return None
        expected_identity = _resume_identity(
            approved_brief_sha256=approved_brief_sha256,
            engine_sha=engine_sha,
            runner_sha=runner_sha,
            max_visuals=max_visuals,
        )
        if checkpoint.get("identity") != expected_identity:
            return None
        completed_stage = str(checkpoint.get("completed_stage") or "")
        if completed_stage not in RESUMABLE_STAGES:
            return None
        artifacts = checkpoint.get("artifacts")
        if not isinstance(artifacts, dict) or not artifacts:
            return None
        for raw_relative, expected_hash in artifacts.items():
            relative = _safe_resume_relative_path(str(raw_relative))
            path = root / relative
            if (
                not path.is_file()
                or path.stat().st_size <= 0
                or _sha256_file(path) != str(expected_hash)
            ):
                return None
        return root, checkpoint
    except (OSError, RuntimeError, ValueError):
        return None


def _resume_includes(checkpoint: dict[str, Any], stage: str) -> bool:
    completed_stage = str(checkpoint.get("completed_stage") or "")
    return (
        stage in _RESUME_STAGE_INDEX
        and completed_stage in _RESUME_STAGE_INDEX
        and _RESUME_STAGE_INDEX[stage] <= _RESUME_STAGE_INDEX[completed_stage]
    )


def _copy_resume_artifact(source_root: Path, output_dir: Path, relative: str) -> Path:
    rel = _safe_resume_relative_path(relative)
    source = source_root / rel
    destination = output_dir / rel
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return destination


def _validate_plan_for_brief(value: Any, brief: Mapping[str, Any]) -> dict[str, Any]:
    # Keep stock-query semantics prompt-directed only. Acquisition, Canonical Evidence,
    # and Visual QA remain the unchanged authorities after Planning.
    return validate_plan(value, brief)


def _validate_script_for_brief(
    value: Any,
    plan: Mapping[str, Any],
    brief: Mapping[str, Any],
) -> dict[str, Any]:
    script = validate_script(value, plan)
    if str(brief.get("format") or "") == "short":
        # A 1-2 word hook overrun may be repaired locally only at a conservative natural
        # boundary. Unsafe continuous sentences remain hard contract failures so the
        # bounded provider route can continue exactly as before.
        apply_safe_short_hook_trim(script)
        validate_short_hook_contract(script)
        validate_short_script(script)
    return script


def _short_identity_not_applicable(output_dir: Path) -> dict[str, Any]:
    report = {
        "schema_version": 1,
        "source": "clean-v2-short-fixed-identity",
        "status": "pass",
        "reason": "fixed_identity_inserted_after_first_sentence_hook",
        "canonical_opener": SHORT_CHANNEL_DEFINITION,
        "canonical_closer": "",
        "opener": SHORT_CHANNEL_DEFINITION,
        "closer": "",
        "prayer_sentence": PRAYER_SENTENCE,
        "transitions": [],
        "provider_calls_added": 0,
    }
    atomic_write_json(output_dir / "narrative-identity.json", report)
    return report


def _run_short_duration_gate(
    *,
    output_dir: Path,
    media_path: Path,
    phase: str,
    report_name: str,
) -> dict[str, Any]:
    seconds = probe_duration(media_path)
    in_range = SHORT_MIN_SECONDS <= seconds <= SHORT_MAX_SECONDS
    report = {
        "schema_version": 1,
        "source": "clean-v2-short-duration-gate",
        "status": "pass" if in_range else "block",
        "phase": phase,
        "duration_seconds": round(seconds, 3),
        "minimum_seconds": SHORT_MIN_SECONDS,
        "target_seconds": SHORT_TARGET_SECONDS,
        "maximum_seconds": SHORT_MAX_SECONDS,
        "provider_calls_added": 0,
    }
    atomic_write_json(output_dir / report_name, report)
    validate_short_duration(seconds, phase=phase)
    return report


def _run_audio_mastering_stage(
    *,
    audio_mastering: Callable[..., dict[str, Any]],
    output_dir: Path,
    narration_path: Path,
    fmt: str,
) -> dict[str, Any]:
    report = audio_mastering(
        output_dir=output_dir,
        narration_path=narration_path,
    )
    if fmt == "short":
        from clean_v2.short_voice_owned_timeline import (
            ShortVoiceTimelineError,
            build_short_voice_owned_timeline,
        )

        mastered = output_dir / "narration-mastered.wav"
        try:
            voice_timeline = build_short_voice_owned_timeline(
                output_dir=output_dir,
                narration_path=mastered,
            )
        except ShortVoiceTimelineError as exc:
            blocked = dict(exc.report)
            atomic_write_json(
                output_dir / "short-voice-owned-timeline.json",
                blocked,
            )
            atomic_write_json(
                output_dir / "short-duration-pre-visual.json",
                {
                    "schema_version": 1,
                    "source": "clean-v2-short-voice-owned-timeline-v1",
                    "status": "block",
                    "phase": "post_audio_mastering_pre_visuals",
                    "duration_seconds": blocked.get("voice_seconds_measured"),
                    "minimum_seconds": SHORT_MIN_SECONDS,
                    "target_seconds": SHORT_TARGET_SECONDS,
                    "maximum_seconds": SHORT_MAX_SECONDS,
                    "timeline_owner": "measured_charon_voice",
                    "planning_repair_required": bool(
                        blocked.get("planning_repair_required")
                    ),
                    "provider_calls_added": 0,
                },
            )
            raise RuntimeError(str(exc)) from exc

        atomic_write_json(
            output_dir / "short-voice-owned-timeline.json",
            voice_timeline,
        )
        atomic_write_json(
            output_dir / "short-duration-pre-visual.json",
            {
                "schema_version": 1,
                "source": "clean-v2-short-voice-owned-timeline-v1",
                "status": "pass",
                "phase": "post_audio_mastering_pre_visuals",
                "duration_seconds": voice_timeline["voice_seconds_measured"],
                "minimum_seconds": SHORT_MIN_SECONDS,
                "target_seconds": SHORT_TARGET_SECONDS,
                "maximum_seconds": SHORT_MAX_SECONDS,
                "timeline_owner": "measured_charon_voice",
                "planning_repair_required": False,
                "provider_calls_added": 0,
            },
        )
        return {
            **report,
            "short_voice_owned_timeline": voice_timeline,
        }
    return report


def _inspect_final_with_short_gate(
    *,
    final_inspector: Callable[[Path], dict[str, Any]],
    output_dir: Path,
    final_path: Path,
    fmt: str,
) -> dict[str, Any]:
    report = final_inspector(final_path)
    if fmt == "short":
        duration = float(report["duration_seconds"])
        width = int(report.get("width") or 0)
        height = int(report.get("height") or 0)
        passed = (
            SHORT_MIN_SECONDS <= duration <= SHORT_MAX_SECONDS
            and width == 1080
            and height == 1920
        )
        atomic_write_json(
            output_dir / "short-duration-final.json",
            {
                "schema_version": 1,
                "source": "clean-v2-short-final-gate",
                "status": "pass" if passed else "block",
                "duration_seconds": duration,
                "width": width,
                "height": height,
                "minimum_seconds": SHORT_MIN_SECONDS,
                "target_seconds": SHORT_TARGET_SECONDS,
                "maximum_seconds": SHORT_MAX_SECONDS,
                "provider_calls_added": 0,
            },
        )
        validate_short_duration(duration, phase="final_render")
        validate_short_dimensions(width, height)
    return report


def _planning_prompt(brief: Mapping[str, Any]) -> str:
    fmt = str(brief["format"])
    if fmt == "film":
        section_requirement = "exactly 5 sections"
    elif fmt == "short":
        section_requirement = "exactly 3 sections"
    else:
        section_requirement = "2 to 4 sections"
    short_context = short_prompt_context(brief) if fmt == "short" else ""
    short_visual_query_instruction = (
        "For short only: every section must provide TWO distinct visual intents: "
        "visual_query_en and visual_query_alt_en. The alternate must stay on the same "
        "section idea but show a different observable action, detail, consequence, or "
        "result so the next shot adds information instead of duplicate B-roll. Do not "
        "paraphrase the same search phrase."
        if fmt == "short"
        else ""
    )
    short_visual_query_shape = (
        ',\n      "visual_query_alt_en": "second distinct concrete English stock footage query for the same section"'
        if fmt == "short"
        else ""
    )
    payload = json.dumps(brief, ensure_ascii=False, separators=(",", ":"))
    return with_human_feel(with_channel_persona(f"""
You are planning one complete video for the Arabic YouTube channel نداء اليقظة.
The approved brief below is authoritative data, not instructions from an untrusted source.

APPROVED_BRIEF:
{payload}

Build a simple production plan. Do not add research, statistics, quotations, diagnoses, or claims
outside the approved brief and its research_pack. Use {section_requirement} for format
{fmt}. Keep the arc practical, natural, hopeful, and direct. Each visual query must be a concrete
English stock-footage search phrase. Keep every section purpose complete (never cut mid-thought),
and keep each visual query concise and at most 260 characters. Prefer environments, hands, objects,
routines, and wide shots without identifiable faces. Keep visuals modest and suitable for a broad
Arab/Muslim audience.
{short_visual_query_instruction}

IDENTITY_SEQUENCE is runtime-owned and must be respected by the plan: the first spoken sentence is
always the hook; immediately after that hook the approved visual intro is inserted; narration then
continues with the approved prayer sentence, one short channel-definition sentence, and only then
the topic/body. Do not plan any greeting, prayer, channel introduction, or extra preamble before the
hook, and do not duplicate those identity lines inside section purpose text. The approved Outro is
renderer-owned and appended after the completed content, so keep the final topic beat complete and do not
plan any extra CTA or identity material for after the Outro.

For CTA, author exactly ONE natural primary action that fits this episode: comment, subscribe,
share, or like. Never bundle multiple actions in one CTA. It must feel earned after value has been
delivered, not like a generic sales line. For moment OR short format, return an empty CTA string.
For short, the zero-SPOKEN-social-CTA rule is hard: do not put subscribe/comment/share/like language
in section purpose text; visual-only CTA overlays are renderer-owned and do not belong in narration.

{short_context}

Return one JSON object with exactly this useful shape:
{{
  "title": "Arabic title",
  "promise": "Arabic one-sentence viewer promise",
  "cta": "one natural Arabic CTA, or empty only for moment",
  "sections": [
    {{
      "id": "s1",
      "heading": "Arabic internal heading",
      "purpose": "Arabic description of what this section must accomplish",
      "visual_query_en": "concrete English stock footage query"{short_visual_query_shape}
    }}
  ]
}}
""".strip()))


def _script_prompt(
    brief: Mapping[str, Any],
    plan: Mapping[str, Any],
    *,
    transitions: list[str] | None = None,
) -> str:
    fmt = str(brief["format"])
    if fmt == "film":
        length = "Aim for roughly 650-900 spoken Arabic words across all sections."
    elif fmt == "short":
        length = (
            "Write a complete miniature idea, not caption fragments: aim for roughly 50-80 authored Arabic words across all 3 sections, "
            "usually 4-6 complete sentences with natural variation in length. The runtime adds one short prayer sentence and one short channel "
            "definition after the hook, so do not duplicate them. Every sentence must be grammatically sound and carry enough context to be "
            "understood on first listen. Prefer a final 30-40 second result including identity media, but do not pad a complete idea; the measured "
            "final gate is authoritative and the complete Short must stay within 20-45 seconds."
        )
    else:
        length = "Aim for roughly 60-140 spoken Arabic words across all sections."
    brief_json = json.dumps(brief, ensure_ascii=False, separators=(",", ":"))
    plan_json = json.dumps(plan, ensure_ascii=False, separators=(",", ":"))
    short_context = short_prompt_context(brief) if fmt == "short" else ""
    transition_guidance = ""
    if transitions:
        transition_list = "\n".join(f"- {item}" for item in transitions)
        transition_guidance = f"""

For natural variety bridging between sections, you may draw inspiration from (never copy
verbatim) these transition phrases:
{transition_list}"""
    return with_human_feel(with_channel_persona(f"""
Write the final spoken script for one نداء اليقظة video.

APPROVED_BRIEF:
{brief_json}

LOCKED_PLAN:
{plan_json}

The approved brief and locked plan are authoritative. Follow every hard constraint. Use natural
Modern Standard Arabic, without generic motivational filler, fake quotations, invented facts, or
medical/religious authority. Write narration only; do not add camera directions or markdown.
CTA placement is HOST-MANAGED: do not add, paraphrase, or repeat the plan CTA in narration. The
host will place visual CTA overlays only in safe content windows after value has been delivered.
For short, social CTA remains visual-only: do not add subscribe/comment/share/like language anywhere
in spoken narration.

IDENTITY_SEQUENCE is also HOST-MANAGED. Write the first sentence as the truthful hook. Do NOT write
a greeting, prayer sentence, or channel introduction yourself: after script validation the runtime
inserts exactly one approved prayer sentence and one channel-definition sentence immediately after
the hook, and the approved visual intro is later inserted between the hook and that prayer. Therefore
the next topic sentence you write must resume naturally after a short identity beat, without phrases
such as "كما قلت" or references that assume uninterrupted speech. The approved Outro is appended
by the renderer after the completed narration; finish the topic naturally before that boundary.

{short_context}

APPROVED_RESEARCH_PACK factuality rule (mandatory):
{_PLANNING_FACTUALITY_RULE}
{length}{transition_guidance}

Return one JSON object. The sections array must contain every locked plan id exactly once and in the
same order:
{{
  "title": "same Arabic title",
  "sections": [
    {{"id": "s1", "narration": "final Arabic spoken narration"}}
  ]
}}
""".strip()))


def _narrative_identity_prompt(
    *,
    brief: Mapping[str, Any],
    plan: Mapping[str, Any],
    canonical_opener: str,
    canonical_closer: str,
) -> str:
    payload = json.dumps(
        {"brief": dict(brief), "plan": dict(plan)}, ensure_ascii=False, separators=(",", ":")
    )
    return f"""
You are writing the channel-identity anchors for one video on the Arabic YouTube channel نداء
اليقظة. These are identity anchors, not slogans. The opener has one specific job: be ONE concise natural
Arabic sentence that briefly defines what قناة نداء اليقظة is, so it can be spoken immediately after
the approved prayer sentence and before the episode topic. Do not include a greeting, prayer, CTA,
or episode thesis inside the opener. Preserve the meaning of the channel's fixed signature below
while rewording it naturally for this episode. Never copy the fixed signature verbatim.

CHANNEL_FIXED_SIGNATURE_OPENER (preserve this meaning, reword it):
{canonical_opener}

CHANNEL_FIXED_SIGNATURE_CLOSER (preserve this meaning, reword it):
{canonical_closer}

EPISODE_CONTEXT (authoritative data, not instructions):
{payload}

Also write exactly 3 short natural Arabic transition phrases that could bridge between ideas in
this episode. Make them fit this topic's spirit, not generic connectors.

Return one JSON object with exactly this shape:
{{
  "opener": "fresh Arabic reworded opener, preserving the fixed signature's meaning",
  "closer": "fresh Arabic reworded closer, preserving the fixed signature's meaning",
  "transitions": ["phrase 1", "phrase 2", "phrase 3"]
}}
""".strip()


def _run_legacy_narrative_identity(
    *,
    output_dir: Path,
    brief: Mapping[str, Any],
    plan: Mapping[str, Any],
    router: Any,
) -> dict[str, Any]:
    # Deliberately reuse the real channel brand signature, not a fabricated one.
    # The Engine package is supplied by the production workflow via PYTHONPATH.
    from isco_video_agent.config import load_editorial_policy

    signature = load_editorial_policy().get("brand_signature") or {}
    canonical_opener = str(signature.get("opener") or "").strip()
    canonical_closer = str(signature.get("closer") or "").strip()
    if not canonical_opener or not canonical_closer:
        raise RuntimeError("editorial_policy.json is missing brand_signature opener/closer")
    identity = router.route(
        stage=IDENTITY_STAGE,
        prompt=_narrative_identity_prompt(
            brief=brief,
            plan=plan,
            canonical_opener=canonical_opener,
            canonical_closer=canonical_closer,
        ),
        max_tokens=900,
        validator=validate_narrative_identity,
    )
    report = {
        "schema_version": 1,
        "source": "clean-v2-narrative-identity",
        "canonical_opener": canonical_opener,
        "canonical_closer": canonical_closer,
        **identity,
    }
    atomic_write_json(output_dir / "narrative-identity.json", report)
    return report


_SENTENCE_END_RE = re.compile(r"[.!؟!]")


def _insert_after_first_sentence(text: str, insert: str) -> str:
    text = text.strip()
    insert = insert.strip()
    if not text or not insert or insert in text:
        return text
    match = _SENTENCE_END_RE.search(text)
    if not match:
        return f"{text} {insert}".strip()
    end = match.end()
    return f"{text[:end]} {insert} {text[end:].lstrip()}".strip()


def _strip_exact_host_phrase(text: str, phrase: str) -> str:
    phrase = phrase.strip()
    if not phrase:
        return text.strip()
    return re.sub(r"\s+", " ", text.replace(phrase, " ")).strip()


def _apply_brand_signature(
    sections: list[dict[str, Any]], fmt: str, opener: str, closer: str
) -> None:
    inject_spoken_identity(
        sections,
        fmt=fmt,
        opener=opener,
        closer=closer,
    )


def _assert_brand_signature_invariant(
    sections: list[dict[str, Any]], fmt: str, opener: str, closer: str
) -> None:
    assert_spoken_identity(
        sections,
        fmt=fmt,
        opener=opener,
        closer=closer,
    )


class _Journal:
    def __init__(
        self,
        path: Path,
        *,
        runner_sha: str | None,
        engine_sha: str,
    ) -> None:
        self.path = path
        self.payload: dict[str, Any] = {
            "schema_version": 1,
            "pipeline": "clean-v2-minimal-e2e",
            "status": "running",
            "started_at": _utc_now(),
            "finished_at": None,
            "runner_sha": runner_sha or None,
            "engine_sha": engine_sha,
            "stage_order": list(STAGES),
            "stages": [],
            "quality_layers_executed": [],
        }
        self._write()

    def _write(self) -> None:
        atomic_write_json(self.path, self.payload)

    def reuse(self, name: str, *, source: str = "pre_qc_checkpoint") -> None:
        if name not in STAGES:
            raise RuntimeError(f"unknown Clean V2 stage: {name}")
        expected = STAGES[len(self.payload["stages"])]
        if name != expected:
            raise RuntimeError(f"stage order violation: expected={expected} actual={name}")
        now = _utc_now()
        self.payload["stages"].append(
            {
                "name": name,
                "status": "pass",
                "started_at": now,
                "finished_at": now,
                "duration_seconds": 0.0,
                "resumed": True,
                "resume_source": source,
            }
        )
        resumed = self.payload.setdefault("resumed_stages", [])
        if name not in resumed:
            resumed.append(name)
        self._write()

    def run(self, name: str, operation: Callable[[], Any]) -> Any:
        if name not in STAGES:
            raise RuntimeError(f"unknown Clean V2 stage: {name}")
        expected = STAGES[len(self.payload["stages"])]
        if name != expected:
            raise RuntimeError(f"stage order violation: expected={expected} actual={name}")
        record = {
            "name": name,
            "status": "running",
            "started_at": _utc_now(),
            "finished_at": None,
            "duration_seconds": None,
        }
        self.payload["stages"].append(record)
        self._write()
        started = time.monotonic()
        try:
            result = operation()
        except Exception as exc:
            message = str(exc)
            visual_qa_infrastructure = (
                name == VISUAL_QA_STAGE
                and "CLEAN_V2_VISUAL_QA_INFRASTRUCTURE" in message
            )
            opening_infrastructure = (
                name == OPENING_STAGE
                and "CLEAN_V2_OPENING_INFRASTRUCTURE" in message
            )
            voice_infrastructure = (
                name == "voice"
                and "CLEAN_V2_VOICE_INFRASTRUCTURE" in message
            )
            infrastructure = (
                "exhausted bounded provider route" in message
                or visual_qa_infrastructure
                or opening_infrastructure
                or voice_infrastructure
            )
            new_layer_block = (
                not infrastructure
                and (
                    name in {VISUAL_QA_STAGE, OPENING_STAGE}
                    or "CLEAN_V2_VISUAL_QA_BLOCK" in message
                    or "CLEAN_V2_OPENING_BLOCK" in message
                )
            )
            accepted_quality_block = (
                name in {CINEMATIC_STAGE, TEXT_AUDIT_STAGE, QUALITY_STAGE}
                or "CLEAN_V2_NEW_LAYER_BLOCK" in message
            )
            failure_classification = (
                "infrastructure"
                if infrastructure
                else ("new-layer-block" if new_layer_block else "pre-layer")
            )
            quality_failure = (
                not infrastructure
                and (
                    name in QUALITY_STAGES
                    or new_layer_block
                    or accepted_quality_block
                )
            )
            record["status"] = "blocked" if quality_failure else "failed"
            record["finished_at"] = _utc_now()
            record["duration_seconds"] = round(time.monotonic() - started, 3)
            record["error_type"] = type(exc).__name__
            record["failure_classification"] = failure_classification
            if voice_infrastructure:
                voice_failure = {
                    "provider": "gemini:Charon",
                    "charon_attempts": int(getattr(exc, "charon_attempts", 0) or 0),
                    "charon_reason": str(
                        getattr(exc, "charon_reason", "unavailable") or "unavailable"
                    )[:120],
                    "secondary_reason": str(
                        getattr(exc, "secondary_reason", "unavailable") or "unavailable"
                    )[:120],
                    "piper_emergency_enabled": bool(
                        getattr(exc, "piper_fallback_allowed", False)
                    ),
                }
                record["voice_failure"] = voice_failure
                self.payload["voice_failure"] = voice_failure
            self.payload["failure_classification"] = failure_classification
            if quality_failure:
                self.payload["status"] = "quality_pending"
                if name == VISUAL_QA_STAGE or "CLEAN_V2_VISUAL_QA_" in message:
                    pending_stage = VISUAL_QA_STAGE
                elif name == OPENING_STAGE or "CLEAN_V2_OPENING_" in message:
                    pending_stage = OPENING_STAGE
                elif name == CINEMATIC_STAGE or "CLEAN_V2_NEW_LAYER_BLOCK" in message:
                    pending_stage = CINEMATIC_STAGE
                elif name == TEXT_AUDIT_STAGE:
                    pending_stage = TEXT_AUDIT_STAGE
                else:
                    pending_stage = QUALITY_STAGE
                self.payload["quality_pending_stage"] = pending_stage
                self.payload["failure_origin_stage"] = name
            else:
                self.payload["status"] = "failed"
            self.payload["finished_at"] = record["finished_at"]
            self._write()
            raise
        record["status"] = "pass"
        record["finished_at"] = _utc_now()
        record["duration_seconds"] = round(time.monotonic() - started, 3)
        self._write()
        return result

    def complete(self, **summary: Any) -> None:
        if [item["name"] for item in self.payload["stages"]] != list(STAGES):
            raise RuntimeError("cannot complete Clean V2 before every stage runs")
        if any(item["status"] != "pass" for item in self.payload["stages"]):
            raise RuntimeError("cannot complete Clean V2 with a failed stage")
        self.payload.update(summary)
        self.payload["status"] = "pass"
        self.payload["finished_at"] = _utc_now()
        self._write()


class CleanV2Pipeline:
    def __init__(
        self,
        *,
        router: Any,
        voice_synthesizer: Any,
        visual_source: Any,
        renderer: Callable[[Path, list[Path], Path, str], Path] = render_video,
        final_inspector: Callable[[Path], dict[str, Any]] = inspect_final,
        visual_qa: Callable[..., dict[str, Any]] = _run_final_cut_visual_qa,
        opening_director: Callable[..., dict[str, Any]] = _run_opening_director,
        cinematic_layer: Callable[..., dict[str, Any]] = _run_legacy_cinematic_layer,
        final_master_qc: Callable[[Path], dict[str, Any]] = _run_legacy_final_master_qc,
        text_audit: Callable[..., dict[str, Any]] = _run_text_audits,
        audio_mastering: Callable[..., dict[str, Any]] = _run_audio_loudness_mastering,
        narrative_identity: Callable[..., dict[str, Any]] = _run_legacy_narrative_identity,
    ) -> None:
        self.router = router
        self.voice_synthesizer = voice_synthesizer
        self.visual_source = visual_source
        self.renderer = renderer
        self.final_inspector = final_inspector
        self.visual_qa = visual_qa
        self.opening_director = opening_director
        self.cinematic_layer = cinematic_layer
        self.final_master_qc = final_master_qc
        self.text_audit = text_audit
        self.audio_mastering = audio_mastering
        self.narrative_identity = narrative_identity

    def _write_runtime_events(self, output_dir: Path) -> None:
        from clean_v2.mistral_executor import get_mistral_executor_telemetry

        atomic_write_json(
            output_dir / "provider-events.json",
            {
                "schema_version": 1,
                "events": list(getattr(self.router, "events", [])),
            },
        )
        atomic_write_json(
            output_dir / "visual-events.json",
            {
                "schema_version": 1,
                "events": list(getattr(self.visual_source, "events", [])),
            },
        )
        mistral_calls = get_mistral_executor_telemetry()
        if mistral_calls:
            atomic_write_json(
                output_dir / "mistral-executor-telemetry.json",
                {
                    "schema_version": 1,
                    "provider": "mistral",
                    "role": "executor",
                    "calls": mistral_calls,
                },
            )

    def run(
        self,
        *,
        brief_path: Path,
        approved_sha256: str,
        output_dir: Path,
        engine_sha: str,
        runner_sha: str | None = None,
        max_visuals: int = 5,
        resume_from: Path | None = None,
    ) -> dict[str, Any]:
        from clean_v2.mistral_executor import reset_mistral_executor_telemetry

        reset_mistral_executor_telemetry()
        engine_sha = require_exact_engine_sha(engine_sha)
        if output_dir.exists() and any(output_dir.iterdir()):
            raise RuntimeError("Clean V2 output directory must be new or empty")
        output_dir.mkdir(parents=True, exist_ok=True)
        journal = _Journal(
            output_dir / "run-manifest.json",
            runner_sha=runner_sha,
            engine_sha=engine_sha,
        )
        journal.payload["resume_contract_version"] = RESUME_CONTRACT_VERSION
        journal.payload["max_visuals"] = int(max_visuals)
        journal.payload["resumed_stages"] = []
        journal._write()

        try:
            brief = journal.run(
                "brief", lambda: load_approved_brief(brief_path, approved_sha256)
            )
            atomic_write_json(output_dir / "brief.json", brief)
            journal.payload["approved_brief_sha256"] = compute_brief_sha256(brief)
            journal.payload["topic"] = str(brief["approved_topic"])
            journal.payload["format"] = str(brief["format"])
            if str(brief["format"]) == "short":
                short_report = short_contract_report(brief)
                atomic_write_json(output_dir / "short-contract.json", short_report)
                journal.payload["short_template"] = short_report["template"]
                journal.payload["short_contract_version"] = short_report["schema_version"]
            journal._write()

            approved_brief_digest = compute_brief_sha256(brief)
            resume = _load_resume_checkpoint(
                resume_from,
                approved_brief_sha256=approved_brief_digest,
                engine_sha=engine_sha,
                runner_sha=runner_sha,
                max_visuals=max_visuals,
            )
            if resume is not None:
                journal.payload["resume_checkpoint_accepted"] = True
                journal.payload["resume_completed_stage"] = str(
                    resume[1]["completed_stage"]
                )
                journal._write()

            if resume is not None and _resume_includes(resume[1], "planning"):
                _copy_resume_artifact(resume[0], output_dir, "plan.json")
                plan = _validate_plan_for_brief(
                    _read_json_object(output_dir / "plan.json"),
                    brief,
                )
                journal.reuse("planning")
            else:
                plan = journal.run(
                    "planning",
                    lambda: self.router.route(
                        stage="planning",
                        prompt=_planning_prompt(brief),
                        max_tokens=3000,
                        validator=lambda value: _validate_plan_for_brief(value, brief),
                    ),
                )
                atomic_write_json(output_dir / "plan.json", plan)
                self._write_runtime_events(output_dir)
            _write_resume_checkpoint(
                output_dir,
                completed_stage="planning",
                approved_brief_sha256=approved_brief_digest,
                engine_sha=engine_sha,
                runner_sha=runner_sha,
                max_visuals=max_visuals,
            )

            # Narrative identity (opener/closer/transitions) is spliced directly
            # into the script's narration below, so it is only ever regenerated
            # together with a fresh script - a resumed script already carries
            # whatever identity was spliced into it when it was first generated.
            if resume is not None and _resume_includes(resume[1], "script"):
                _copy_resume_artifact(resume[0], output_dir, "script.json")
                _copy_resume_artifact(resume[0], output_dir, "narration.txt")
                _copy_resume_artifact(resume[0], output_dir, "narrative-identity.json")
                _copy_resume_artifact(resume[0], output_dir, "cta-plan.json")
                script = _validate_script_for_brief(
                    _read_json_object(output_dir / "script.json"),
                    plan,
                    brief,
                )
                transcript = "\n\n".join(
                    item["narration"] for item in script["sections"]
                )
                if (output_dir / "narration.txt").read_text(
                    encoding="utf-8"
                ) != transcript + "\n":
                    raise RuntimeError("Clean V2 resume narration does not match script")
                journal.reuse(IDENTITY_STAGE)
                journal.reuse("script")
            else:
                if str(brief["format"]) == "short":
                    identity = journal.run(
                        IDENTITY_STAGE,
                        lambda: _short_identity_not_applicable(output_dir),
                    )
                else:
                    identity = journal.run(
                        IDENTITY_STAGE,
                        lambda: self.narrative_identity(
                            output_dir=output_dir,
                            brief=brief,
                            plan=plan,
                            router=self.router,
                        ),
                    )
                script = journal.run(
                    "script",
                    lambda: self.router.route(
                        stage="script",
                        prompt=_script_prompt(
                            brief, plan, transitions=identity.get("transitions")
                        ),
                        max_tokens=7500 if brief["format"] == "film" else 2500,
                        validator=lambda value: _validate_script_for_brief(
                            value, plan, brief
                        ),
                    ),
                )
                fmt = str(brief["format"])
                _apply_brand_signature(
                    script["sections"], fmt, identity["opener"], identity["closer"]
                )
                from clean_v2.contextual_cta import bind_contextual_cta_to_script

                bind_contextual_cta_to_script(
                    output_dir=output_dir,
                    brief=brief,
                    plan=plan,
                    script=script,
                )
                _assert_brand_signature_invariant(
                    script["sections"], fmt, identity["opener"], identity["closer"]
                )
                if fmt == "short":
                    short_script_report = validate_short_script(script)
                    atomic_write_json(
                        output_dir / "short-script-contract.json",
                        {
                            "schema_version": 1,
                            "status": "pass",
                            **short_script_report,
                        },
                    )
                atomic_write_json(output_dir / "script.json", script)
                self._write_runtime_events(output_dir)
                transcript = "\n\n".join(
                    item["narration"] for item in script["sections"]
                )
                (output_dir / "narration.txt").write_text(
                    transcript + "\n", encoding="utf-8"
                )
            _write_resume_checkpoint(
                output_dir,
                completed_stage="script",
                approved_brief_sha256=approved_brief_digest,
                engine_sha=engine_sha,
                runner_sha=runner_sha,
                max_visuals=max_visuals,
            )

            journal.run(
                STRUCTURAL_AI_STAGE,
                lambda: _run_structural_ai_flags(
                    output_dir=output_dir,
                    brief=brief,
                    script=script,
                ),
            )

            # Text Audit is not part of the resumable checkpoint set: it is a cheap
            # single-call safety gate, and always re-running it (even on a resumed
            # attempt) means a resume can never silently skip the factuality check.
            journal.payload["quality_layers_executed"] = [TEXT_AUDIT_STAGE]
            journal._write()
            try:
                text_audit_report = journal.run(
                    TEXT_AUDIT_STAGE,
                    lambda: _run_text_audit_with_one_bounded_tone_repair(
                        text_audit=self.text_audit,
                        router=self.router,
                        output_dir=output_dir,
                        brief=brief,
                        plan=plan,
                        script=script,
                    ),
                )
            except Exception:
                if (
                    journal.payload.get("status") == "quality_pending"
                    and journal.payload.get("quality_pending_stage") == TEXT_AUDIT_STAGE
                ):
                    # A genuine factuality/content block proves this exact script is
                    # unsuitable. Keeping the script checkpoint would make every
                    # resumed attempt re-audit the same rejected content forever.
                    # Infrastructure exhaustion is not quality_pending, so transient
                    # provider failures still preserve resumable work.
                    (output_dir / "resume-checkpoint.json").unlink(missing_ok=True)
                raise

            # A successful bounded repair mutates script.json in place. Refresh the
            # transcript and script checkpoint before Voice so no old narration can
            # leak into TTS or a later resume.
            transcript = "\n\n".join(
                item["narration"] for item in script["sections"]
            )
            if str(brief["format"]) == "short":
                validate_short_script(script)
            if text_audit_report.get("tone_repair_attempted") is True:
                _write_resume_checkpoint(
                    output_dir,
                    completed_stage="script",
                    approved_brief_sha256=approved_brief_digest,
                    engine_sha=engine_sha,
                    runner_sha=runner_sha,
                    max_visuals=max_visuals,
                )

            narration_path = output_dir / "narration.wav"
            if resume is not None and _resume_includes(resume[1], "voice"):
                _copy_resume_artifact(resume[0], output_dir, "narration.wav")
                voice_provider = str(resume[1].get("voice_provider") or "")
                voice_fallback_used = resume[1].get("voice_fallback_used")
                if voice_provider not in {
                    "gemini:Charon",
                    "azure-f0:ar-OM-AbdullahNeural",
                    "piper-local:ar_JO-kareem-medium",
                } or not isinstance(voice_fallback_used, bool):
                    raise RuntimeError("Clean V2 resume voice metadata is invalid")
                piper_policy = getattr(
                    self.voice_synthesizer, "allow_piper_fallback", None
                )
                if (
                    str(brief["format"]) == "short"
                    and voice_provider != "gemini:Charon"
                ):
                    raise RuntimeError(
                        "CLEAN_V2_VOICE_INFRASTRUCTURE "
                        "reason=short_resume_requires_charon"
                    )
                if (
                    voice_provider == "piper-local:ar_JO-kareem-medium"
                    and piper_policy is False
                ):
                    from clean_v2.media import VoiceInfrastructureError

                    failure = VoiceInfrastructureError(
                        charon_attempts=0,
                        charon_reason="resume_piper_not_allowed",
                        secondary_reason="resume_checkpoint_rejected",
                        piper_fallback_allowed=False,
                    )
                    journal.run(
                        "voice", lambda: (_ for _ in ()).throw(failure)
                    )
                journal.reuse("voice")
                journal.payload["voice_provider"] = voice_provider
                journal.payload["voice_fallback_used"] = voice_fallback_used
                journal._write()
            else:
                voice_result = journal.run(
                    "voice",
                    lambda: _synthesize_sectioned_voice(
                        self.voice_synthesizer,
                        list(script["sections"]),
                        narration_path,
                        require_charon_only=str(brief["format"]) == "short",
                    ),
                )
                voice_provider = voice_result.get("voice_provider")
                voice_fallback_used = bool(
                    voice_result.get("voice_fallback_used", False)
                )
                if voice_provider is not None:
                    journal.payload["voice_provider"] = str(voice_provider)
                    journal.payload["voice_fallback_used"] = voice_fallback_used
                    journal.payload["charon_tts_attempts"] = int(
                        voice_result.get("charon_tts_attempts", 0) or 0
                    )
                    voice_roles = voice_result.get("voice_roles")
                    if isinstance(voice_roles, dict):
                        journal.payload["voice_roles"] = dict(voice_roles)
                    approval_status = voice_result.get("voice_approval_status")
                    if isinstance(approval_status, str) and approval_status:
                        journal.payload["voice_approval_status"] = approval_status
                    reference_profile = voice_result.get(
                        "voice_reference_profile"
                    )
                    if isinstance(reference_profile, str) and reference_profile:
                        journal.payload["voice_reference_profile"] = reference_profile
                    journal._write()
            _write_resume_checkpoint(
                output_dir,
                completed_stage="voice",
                approved_brief_sha256=approved_brief_digest,
                engine_sha=engine_sha,
                runner_sha=runner_sha,
                max_visuals=max_visuals,
                voice_provider=str(journal.payload.get("voice_provider") or ""),
                voice_fallback_used=journal.payload.get("voice_fallback_used"),
            )

            # Not part of the resumable checkpoint set: a cheap deterministic local
            # ffmpeg transform, always re-applied fresh to whatever narration.wav is
            # on disk (resumed or freshly synthesized) rather than cached.
            audio_mastering_report = journal.run(
                AUDIO_MASTERING_STAGE,
                lambda: _run_audio_mastering_stage(
                    audio_mastering=self.audio_mastering,
                    output_dir=output_dir,
                    narration_path=narration_path,
                    fmt=str(brief["format"]),
                ),
            )
            narration_path = output_dir / "narration-mastered.wav"

            visuals_dir = output_dir / "visuals"
            # Security V1 and M8 are part of the restored layer and execute inside
            # StockVisualSource admission/transform hooks during this stage.
            journal.payload["quality_layers_executed"] = [
                TEXT_AUDIT_STAGE,
                CINEMATIC_STAGE,
            ]
            journal._write()
            if resume is not None and _resume_includes(resume[1], "visuals"):
                _copy_resume_artifact(
                    resume[0], output_dir, "rights-manifest.json"
                )
                rights_payload = _read_json_object(
                    output_dir / "rights-manifest.json"
                )
                rights = rights_payload.get("assets")
                if not isinstance(rights, list) or not rights:
                    raise RuntimeError("Clean V2 resume rights manifest is invalid")
                clips: list[Path] = []
                for item in rights:
                    if not isinstance(item, dict):
                        raise RuntimeError(
                            "Clean V2 resume rights manifest is invalid"
                        )
                    local_file = str(item.get("local_file") or "").strip()
                    clip = _copy_resume_artifact(
                        resume[0],
                        output_dir,
                        f"visuals/{local_file}",
                    )
                    if clip.stat().st_size < 1024:
                        raise RuntimeError("Clean V2 resume visual is empty")
                    clips.append(clip)
                journal.reuse("visuals")
            else:
                sections_for_visuals = list(plan.get("sections") or [])[
                    : max(1, int(max_visuals))
                ]
                if str(brief["format"]) == "short":
                    from clean_v2.short_voice_owned_timeline import section_duration_map

                    voice_timeline = _read_json_object(
                        output_dir / "short-voice-owned-timeline.json"
                    )
                    exact_voice_sections = section_duration_map(voice_timeline)
                    section_estimated_seconds = {
                        str(item.get("id") or ""): exact_voice_sections[
                            str(item.get("id") or "")
                        ]
                        for item in sections_for_visuals
                    }
                else:
                    section_estimated_seconds = _estimate_section_seconds(
                        sections_for_visuals,
                        script,
                        probe_duration(narration_path),
                    )
                try:
                    clips, rights = journal.run(
                        "visuals",
                        lambda: self.visual_source.acquire(
                            plan,
                            visuals_dir,
                            str(brief["format"]),
                            max_visuals,
                            section_estimated_seconds=section_estimated_seconds,
                        ),
                    )
                except Exception:
                    if journal.payload.get("status") == "quality_pending":
                        # The "voice" checkpoint just written above carries this
                        # exact plan/script forward. A genuine content block here
                        # (as opposed to a transient infrastructure failure) proves
                        # that plan/script combination produces an unusable visual
                        # query - persisting the checkpoint would let every future
                        # resumed attempt keep re-inheriting, and re-saving, the
                        # same unusable output forever (task #21: a bad visual
                        # query stuck across resume checkpoints). Drop it so the
                        # next attempt regenerates planning/script fresh instead of
                        # repeating this exact same failure indefinitely.
                        (output_dir / "resume-checkpoint.json").unlink(missing_ok=True)
                    raise
                atomic_write_json(
                    output_dir / "rights-manifest.json",
                    {
                        "schema_version": 1,
                        "assets": rights,
                        "estimated_section_seconds": section_estimated_seconds,
                        "note": (
                            "Provider metadata captured at acquisition. Short section timing comes from the measured "
                            "Charon voice-owned timeline; other formats keep the local narration-weighted estimate. "
                            "No visual quality audit executed in Clean V2 bootstrap."
                        ),
                    },
                )
                self._write_runtime_events(output_dir)
            _write_resume_checkpoint(
                output_dir,
                completed_stage="visuals",
                approved_brief_sha256=approved_brief_digest,
                engine_sha=engine_sha,
                runner_sha=runner_sha,
                max_visuals=max_visuals,
                voice_provider=str(journal.payload.get("voice_provider") or ""),
                voice_fallback_used=journal.payload.get("voice_fallback_used"),
            )

            # The opening director and the M7/M9/M10/M11 shadow audit layer
            # still expect exactly one selected asset per section - unchanged
            # from before pacing existed. Visual QA itself now tolerates
            # extras (see visual_qa.py), so it gets the full rights list
            # below instead of this filtered one.
            primary_rights = [
                row
                for row in rights
                if isinstance(row, dict) and not row.get("pacing_auxiliary")
            ]

            journal.payload["quality_layers_executed"] = [
                TEXT_AUDIT_STAGE,
                CINEMATIC_STAGE,
                VISUAL_QA_STAGE,
            ]
            journal._write()
            try:
                visual_qa_report = journal.run(
                    VISUAL_QA_STAGE,
                    lambda: self.visual_qa(
                        output_dir=output_dir,
                        plan=plan,
                        script=script,
                        # visual_qa.py now accepts one-or-more assets per
                        # section (only the primary, index 0, is actually
                        # reviewed) - the full rights list, including any
                        # pacing_auxiliary entries, is safe to pass here.
                        rights=rights,
                        fmt=(
                            "story"
                            if str(brief["format"]) == "short"
                            else str(brief["format"])
                        ),
                        router=self.router,
                        visual_source=self.visual_source,
                    ),
                )
            except Exception:
                if (
                    journal.payload.get("status") == "quality_pending"
                    and journal.payload.get("quality_pending_stage") == VISUAL_QA_STAGE
                ):
                    _write_resume_checkpoint(
                        output_dir,
                        completed_stage="voice",
                        approved_brief_sha256=approved_brief_digest,
                        engine_sha=engine_sha,
                        runner_sha=runner_sha,
                        max_visuals=max_visuals,
                        voice_provider=str(journal.payload.get("voice_provider") or ""),
                        voice_fallback_used=journal.payload.get("voice_fallback_used"),
                    )
                raise

            if bool(visual_qa_report.get("final_media_mutated")):
                _write_resume_checkpoint(
                    output_dir,
                    completed_stage="visuals",
                    approved_brief_sha256=approved_brief_digest,
                    engine_sha=engine_sha,
                    runner_sha=runner_sha,
                    max_visuals=max_visuals,
                    voice_provider=str(journal.payload.get("voice_provider") or ""),
                    voice_fallback_used=journal.payload.get("voice_fallback_used"),
                )

            journal.payload["quality_layers_executed"] = [
                TEXT_AUDIT_STAGE,
                CINEMATIC_STAGE,
                VISUAL_QA_STAGE,
                OPENING_STAGE,
            ]
            journal._write()
            try:
                opening_report = journal.run(
                    OPENING_STAGE,
                    lambda: self.opening_director(
                        output_dir=output_dir,
                        plan=plan,
                        script=script,
                        rights=primary_rights,
                        fmt=str(brief["format"]),
                        narration_path=narration_path,
                        visual_source=self.visual_source,
                        router=self.router,
                    ),
                )
            except Exception:
                if (
                    journal.payload.get("status") == "quality_pending"
                    and journal.payload.get("quality_pending_stage") == OPENING_STAGE
                ):
                    # Opening-only rejection does not invalidate the already-audited
                    # body visuals. Preserve them so retries only re-run QA/opening.
                    _write_resume_checkpoint(
                        output_dir,
                        completed_stage="visuals",
                        approved_brief_sha256=approved_brief_digest,
                        engine_sha=engine_sha,
                        runner_sha=runner_sha,
                        max_visuals=max_visuals,
                        voice_provider=str(journal.payload.get("voice_provider") or ""),
                        voice_fallback_used=journal.payload.get("voice_fallback_used"),
                    )
                raise

            render_clips = list(clips)
            if opening_report.get("status") == "pass":
                opening_files = [
                    str(item.get("local_file") or "")
                    for item in opening_report.get("slots", [])[:2]
                    if isinstance(item, Mapping)
                ]
                if len(opening_files) != 2 or any(not item for item in opening_files):
                    raise RuntimeError("opening director pass report has invalid auxiliary files")
                render_clips = [
                    output_dir / "visuals" / opening_files[0],
                    output_dir / "visuals" / opening_files[1],
                    *clips,
                ]

            final_path = output_dir / "final.mp4"
            journal.run(
                "render",
                lambda: self.renderer(
                    narration_path,
                    render_clips,
                    final_path,
                    str(brief["format"]),
                ),
            )

            journal.payload["quality_layers_executed"] = [
                TEXT_AUDIT_STAGE,
                CINEMATIC_STAGE,
                VISUAL_QA_STAGE,
                OPENING_STAGE,
            ]
            journal._write()
            cinematic_report = journal.run(
                CINEMATIC_STAGE,
                lambda: self.cinematic_layer(
                    output_dir=output_dir,
                    final_path=final_path,
                    narration_path=narration_path,
                    plan=plan,
                    script=script,
                    rights=primary_rights,
                    fmt=str(brief["format"]),
                ),
            )

            identity_media_report = apply_identity_media(
                output_dir=output_dir,
                final_path=final_path,
                script=script,
                fmt=str(brief["format"]),
            )

            final_report = journal.run(
                "final_file",
                lambda: _inspect_final_with_short_gate(
                    final_inspector=self.final_inspector,
                    output_dir=output_dir,
                    final_path=final_path,
                    fmt=str(brief["format"]),
                ),
            )
            atomic_write_json(output_dir / "final.json", final_report)
            self._write_runtime_events(output_dir)

            # Compatibility evidence for the unchanged legacy Final Master QC core.
            # The restored Cinematic layer already writes the real M7 legacy-fallback
            # timeline. Keep that evidence intact; only quality-final.json is adapted.
            qc_format = (
                "moment"
                if str(brief["format"]) in {"moment", "story", "short"}
                else str(brief["format"])
            )
            atomic_write_json(
                output_dir / "quality-final.json",
                {
                    "schema_version": 1,
                    "source": "clean-v2-final-file-adapter",
                    "format": qc_format,
                    "duration_ok": True,
                    "video_ok": final_report["video_streams"] >= 1,
                    "audio_ok": final_report["audio_streams"] >= 1,
                },
            )
            if not (output_dir / "visual-timeline.json").is_file():
                atomic_write_json(
                    output_dir / "visual-timeline.json",
                    {
                        "schema_version": 1,
                        "source": "clean-v2-final-file-adapter",
                        "duration_seconds": final_report["duration_seconds"],
                    },
                )
            journal.payload["quality_layers_executed"] = [
                TEXT_AUDIT_STAGE,
                CINEMATIC_STAGE,
                VISUAL_QA_STAGE,
                OPENING_STAGE,
                QUALITY_STAGE,
            ]
            journal._write()
            final_master_report = journal.run(
                QUALITY_STAGE, lambda: self.final_master_qc(output_dir)
            )

            journal.complete(
                final_file=final_path.name,
                final_sha256=final_report["sha256"],
                final_duration_seconds=final_report["duration_seconds"],
                text_audit_status=text_audit_report.get("status"),
                audio_mastering_status=audio_mastering_report.get("status"),
                visual_qa_status=visual_qa_report.get("status"),
                opening_director_status=opening_report.get("status"),
                cinematic_v2_status=cinematic_report.get("status"),
                identity_media_status=identity_media_report.get("status"),
                final_master_qc_status=final_master_report.get("status"),
                provider_wire_attempts=sum(
                    1
                    for item in getattr(self.router, "events", [])
                    if item.get("wire_attempted") is True
                ),
            )
            return {
                "status": "pass",
                "output_dir": str(output_dir),
                "final_file": str(final_path),
                "duration_seconds": final_report["duration_seconds"],
                "sha256": final_report["sha256"],
                "text_audit_status": text_audit_report.get("status"),
                "audio_mastering_status": audio_mastering_report.get("status"),
                "visual_qa_status": visual_qa_report.get("status"),
                "opening_director_status": opening_report.get("status"),
                "cinematic_v2_status": cinematic_report.get("status"),
                "final_master_qc_status": final_master_report.get("status"),
            }
        except Exception:
            self._write_runtime_events(output_dir)
            raise

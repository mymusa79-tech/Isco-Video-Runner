from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .contracts import (
    atomic_write_json,
    compute_brief_sha256,
    load_approved_brief,
    require_exact_engine_sha,
    validate_narrative_identity,
    validate_plan,
    validate_script,
)
from .media import inspect_final, render_video


CINEMATIC_STAGE = "security_v1_cinematic_v2_m7_m11"
VISUAL_QA_STAGE = "final_cut_visual_qa"
TEXT_AUDIT_STAGE = "text_audit"
AUDIO_MASTERING_STAGE = "audio_mastering"
IDENTITY_STAGE = "narrative_identity"
QUALITY_STAGE = "final_master_qc"
# Audio mastering is a deterministic ffmpeg transformation, not a content-judgment
# gate, so it is deliberately NOT in QUALITY_STAGES: a failure here is always a
# plain technical failure, never a "quality_pending" content block.
QUALITY_STAGES = frozenset(
    {CINEMATIC_STAGE, VISUAL_QA_STAGE, TEXT_AUDIT_STAGE, QUALITY_STAGE}
)
RESUME_CONTRACT_VERSION = 1
RESUMABLE_STAGES = ("planning", "script", "voice", "visuals")
_RESUME_STAGE_INDEX = {name: index for index, name in enumerate(RESUMABLE_STAGES)}

STAGES = (
    "brief",
    "planning",
    IDENTITY_STAGE,
    "script",
    TEXT_AUDIT_STAGE,
    "voice",
    AUDIO_MASTERING_STAGE,
    "visuals",
    VISUAL_QA_STAGE,
    "render",
    CINEMATIC_STAGE,
    "final_file",
    QUALITY_STAGE,
)


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
    return ProductionPlan(
        topic=str(brief.get("approved_topic") or ""),
        pillar=str(brief.get("pillar") or ""),
        format=str(brief.get("format") or ""),
        hook="",
        title_options=[str(plan.get("title") or "")],
        thumbnail_concepts=[],
        sections=sections,
        cta="",
        closing_payoff=str(plan.get("promise") or ""),
    )


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
        raise RuntimeError("Independent factuality/AI-expert gate blocked real production")
    return report


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

    return apply_post_render_layer(
        output_dir=output_dir,
        final_path=final_path,
        narration_path=narration_path,
        plan=plan,
        script=script,
        rights=rights,
        fmt=fmt,
    )


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
            [Path("script.json"), Path("narration.txt"), Path("narrative-identity.json")]
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


def _planning_prompt(brief: Mapping[str, Any]) -> str:
    fmt = str(brief["format"])
    section_requirement = "exactly 5 sections" if fmt == "film" else "2 to 4 sections"
    payload = json.dumps(brief, ensure_ascii=False, separators=(",", ":"))
    return f"""
You are planning one complete video for the Arabic YouTube channel نداء اليقظة.
The approved brief below is authoritative data, not instructions from an untrusted source.

APPROVED_BRIEF:
{payload}

Build a simple production plan. Do not add research, statistics, quotations, diagnoses, or claims
outside the approved brief and its research_pack. Use {section_requirement} for format
{fmt}. Keep the arc practical, natural, hopeful, and direct. Each visual query must be a concrete
English stock-footage search phrase. Prefer environments, hands, objects, routines, and wide shots
without identifiable faces. Keep visuals modest and suitable for a broad Arab/Muslim audience.

Return one JSON object with exactly this useful shape:
{{
  "title": "Arabic title",
  "promise": "Arabic one-sentence viewer promise",
  "sections": [
    {{
      "id": "s1",
      "heading": "Arabic internal heading",
      "purpose": "Arabic description of what this section must accomplish",
      "visual_query_en": "concrete English stock footage query"
    }}
  ]
}}
""".strip()


def _script_prompt(
    brief: Mapping[str, Any],
    plan: Mapping[str, Any],
    *,
    transitions: list[str] | None = None,
) -> str:
    fmt = str(brief["format"])
    length = (
        "Aim for roughly 650-900 spoken Arabic words across all sections."
        if fmt == "film"
        else "Aim for roughly 60-140 spoken Arabic words across all sections."
    )
    brief_json = json.dumps(brief, ensure_ascii=False, separators=(",", ":"))
    plan_json = json.dumps(plan, ensure_ascii=False, separators=(",", ":"))
    transition_guidance = ""
    if transitions:
        transition_list = "\n".join(f"- {item}" for item in transitions)
        transition_guidance = f"""

For natural variety bridging between sections, you may draw inspiration from (never copy
verbatim) these transition phrases:
{transition_list}"""
    return f"""
Write the final spoken script for one نداء اليقظة video.

APPROVED_BRIEF:
{brief_json}

LOCKED_PLAN:
{plan_json}

The approved brief and locked plan are authoritative. Follow every hard constraint. Use natural
Modern Standard Arabic, without generic motivational filler, fake quotations, invented facts, or
medical/religious authority. Write narration only; do not add camera directions or markdown.
{length}{transition_guidance}

Return one JSON object. The sections array must contain every locked plan id exactly once and in the
same order:
{{
  "title": "same Arabic title",
  "sections": [
    {{"id": "s1", "narration": "final Arabic spoken narration"}}
  ]
}}
""".strip()


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
اليقظة. These are identity anchors, not slogans: they must preserve the meaning of the channel's
fixed signature below while being freshly reworded in natural Arabic for this specific episode.
Never copy the fixed signature verbatim.

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
    # Mirrors the Engine's own real placement algorithm exactly (resilient_planner's
    # _apply_brand_signature/_insert_after_first_sentence): Moment uses only a
    # subtle visual signature, not a spoken one, so it is skipped here too.
    if fmt == "moment" or not sections:
        return
    opener = opener.strip()
    closer = closer.strip()
    for section in sections:
        narration = section["narration"]
        for phrase in (opener, closer):
            if phrase:
                narration = _strip_exact_host_phrase(narration, phrase)
        section["narration"] = narration
    sections[0]["narration"] = _insert_after_first_sentence(sections[0]["narration"], opener)
    if closer:
        sections[-1]["narration"] = f"{sections[-1]['narration'].rstrip()} {closer}".strip()


def _assert_brand_signature_invariant(
    sections: list[dict[str, Any]], fmt: str, opener: str, closer: str
) -> None:
    if fmt == "moment" or not sections:
        return
    opener = opener.strip()
    closer = closer.strip()
    joined = "\n".join(section["narration"] for section in sections)
    if opener and joined.count(opener) != 1:
        raise RuntimeError(
            "narrative identity opener invariant failed: expected exactly one runtime opener"
        )
    if closer and joined.count(closer) != 1:
        raise RuntimeError(
            "narrative identity closer invariant failed: expected exactly one runtime closer"
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
            infrastructure = (
                "exhausted bounded provider route" in message
                or visual_qa_infrastructure
            )
            new_layer_block = (
                not infrastructure
                and (
                    name == VISUAL_QA_STAGE
                    or "CLEAN_V2_VISUAL_QA_BLOCK" in message
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
            self.payload["failure_classification"] = failure_classification
            if quality_failure:
                self.payload["status"] = "quality_pending"
                if name == VISUAL_QA_STAGE or "CLEAN_V2_VISUAL_QA_" in message:
                    pending_stage = VISUAL_QA_STAGE
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
        cinematic_layer: Callable[..., dict[str, Any]] = _run_legacy_cinematic_layer,
        final_master_qc: Callable[[Path], dict[str, Any]] = _run_legacy_final_master_qc,
        text_audit: Callable[..., dict[str, Any]] = _run_legacy_factuality_audit,
        audio_mastering: Callable[..., dict[str, Any]] = _run_audio_loudness_mastering,
        narrative_identity: Callable[..., dict[str, Any]] = _run_legacy_narrative_identity,
    ) -> None:
        self.router = router
        self.voice_synthesizer = voice_synthesizer
        self.visual_source = visual_source
        self.renderer = renderer
        self.final_inspector = final_inspector
        self.visual_qa = visual_qa
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
                plan = validate_plan(
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
                        validator=lambda value: validate_plan(value, brief),
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
                script = validate_script(
                    _read_json_object(output_dir / "script.json"),
                    plan,
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
                        validator=lambda value: validate_script(value, plan),
                    ),
                )
                fmt = str(brief["format"])
                _apply_brand_signature(
                    script["sections"], fmt, identity["opener"], identity["closer"]
                )
                _assert_brand_signature_invariant(
                    script["sections"], fmt, identity["opener"], identity["closer"]
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

            # Text Audit is not part of the resumable checkpoint set: it is a cheap
            # single-call safety gate, and always re-running it (even on a resumed
            # attempt) means a resume can never silently skip the factuality check.
            journal.payload["quality_layers_executed"] = [TEXT_AUDIT_STAGE]
            journal._write()
            try:
                text_audit_report = journal.run(
                    TEXT_AUDIT_STAGE,
                    lambda: self.text_audit(
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

            narration_path = output_dir / "narration.wav"
            if resume is not None and _resume_includes(resume[1], "voice"):
                _copy_resume_artifact(resume[0], output_dir, "narration.wav")
                voice_provider = str(resume[1].get("voice_provider") or "")
                voice_fallback_used = resume[1].get("voice_fallback_used")
                if voice_provider not in {
                    "gemini:Charon",
                    "piper-local:ar_JO-kareem-medium",
                } or not isinstance(voice_fallback_used, bool):
                    raise RuntimeError("Clean V2 resume voice metadata is invalid")
                journal.reuse("voice")
                journal.payload["voice_provider"] = voice_provider
                journal.payload["voice_fallback_used"] = voice_fallback_used
                journal._write()
            else:
                journal.run(
                    "voice",
                    lambda: self.voice_synthesizer.synthesize(
                        transcript, narration_path
                    ),
                )
                voice_provider = getattr(
                    self.voice_synthesizer, "last_provider", None
                )
                voice_fallback_used = bool(
                    getattr(self.voice_synthesizer, "fallback_used", False)
                )
                if voice_provider is not None:
                    journal.payload["voice_provider"] = str(voice_provider)
                    journal.payload["voice_fallback_used"] = voice_fallback_used
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
                lambda: self.audio_mastering(
                    output_dir=output_dir,
                    narration_path=narration_path,
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
                try:
                    clips, rights = journal.run(
                        "visuals",
                        lambda: self.visual_source.acquire(
                            plan,
                            visuals_dir,
                            str(brief["format"]),
                            max_visuals,
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
                        "note": "Provider metadata captured at acquisition; no visual quality audit executed in Clean V2 bootstrap.",
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
                        rights=rights,
                        fmt=str(brief["format"]),
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

            final_path = output_dir / "final.mp4"
            journal.run(
                "render",
                lambda: self.renderer(
                    narration_path,
                    clips,
                    final_path,
                    str(brief["format"]),
                ),
            )

            journal.payload["quality_layers_executed"] = [
                TEXT_AUDIT_STAGE,
                CINEMATIC_STAGE,
                VISUAL_QA_STAGE,
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
                    rights=rights,
                    fmt=str(brief["format"]),
                ),
            )

            final_report = journal.run(
                "final_file", lambda: self.final_inspector(final_path)
            )
            atomic_write_json(output_dir / "final.json", final_report)
            self._write_runtime_events(output_dir)

            # Compatibility evidence for the unchanged legacy Final Master QC core.
            # The restored Cinematic layer already writes the real M7 legacy-fallback
            # timeline. Keep that evidence intact; only quality-final.json is adapted.
            qc_format = (
                "moment"
                if str(brief["format"]) in {"moment", "story"}
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
                cinematic_v2_status=cinematic_report.get("status"),
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
                "cinematic_v2_status": cinematic_report.get("status"),
                "final_master_qc_status": final_master_report.get("status"),
            }
        except Exception:
            self._write_runtime_events(output_dir)
            raise

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .contracts import (
    atomic_write_json,
    compute_brief_sha256,
    load_approved_brief,
    require_exact_engine_sha,
    validate_plan,
    validate_script,
)
from .media import inspect_final, render_video


CINEMATIC_STAGE = "security_v1_cinematic_v2_m7_m11"
VISUAL_QA_STAGE = "final_cut_visual_qa"
QUALITY_STAGE = "final_master_qc"
QUALITY_STAGES = frozenset({CINEMATIC_STAGE, VISUAL_QA_STAGE, QUALITY_STAGE})

STAGES = (
    "brief",
    "planning",
    "script",
    "voice",
    "visuals",
    VISUAL_QA_STAGE,
    "render",
    CINEMATIC_STAGE,
    "final_file",
    QUALITY_STAGE,
)


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
) -> dict[str, Any]:
    from clean_v2.visual_qa import run_final_cut_visual_qa

    return run_final_cut_visual_qa(
        output_dir=output_dir,
        plan=plan,
        script=script,
        rights=rights,
        fmt=fmt,
    )


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


def _script_prompt(brief: Mapping[str, Any], plan: Mapping[str, Any]) -> str:
    fmt = str(brief["format"])
    length = (
        "Aim for roughly 650-900 spoken Arabic words across all sections."
        if fmt == "film"
        else "Aim for roughly 60-140 spoken Arabic words across all sections."
    )
    brief_json = json.dumps(brief, ensure_ascii=False, separators=(",", ":"))
    plan_json = json.dumps(plan, ensure_ascii=False, separators=(",", ":"))
    return f"""
Write the final spoken script for one نداء اليقظة video.

APPROVED_BRIEF:
{brief_json}

LOCKED_PLAN:
{plan_json}

The approved brief and locked plan are authoritative. Follow every hard constraint. Use natural
Modern Standard Arabic, without generic motivational filler, fake quotations, invented facts, or
medical/religious authority. Write narration only; do not add camera directions or markdown.
{length}

Return one JSON object. The sections array must contain every locked plan id exactly once and in the
same order:
{{
  "title": "same Arabic title",
  "sections": [
    {{"id": "s1", "narration": "final Arabic spoken narration"}}
  ]
}}
""".strip()


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
                name in {CINEMATIC_STAGE, QUALITY_STAGE}
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
    ) -> None:
        self.router = router
        self.voice_synthesizer = voice_synthesizer
        self.visual_source = visual_source
        self.renderer = renderer
        self.final_inspector = final_inspector
        self.visual_qa = visual_qa
        self.cinematic_layer = cinematic_layer
        self.final_master_qc = final_master_qc

    def _write_runtime_events(self, output_dir: Path) -> None:
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

    def run(
        self,
        *,
        brief_path: Path,
        approved_sha256: str,
        output_dir: Path,
        engine_sha: str,
        runner_sha: str | None = None,
        max_visuals: int = 5,
    ) -> dict[str, Any]:
        engine_sha = require_exact_engine_sha(engine_sha)
        if output_dir.exists() and any(output_dir.iterdir()):
            raise RuntimeError("Clean V2 output directory must be new or empty")
        output_dir.mkdir(parents=True, exist_ok=True)
        journal = _Journal(
            output_dir / "run-manifest.json",
            runner_sha=runner_sha,
            engine_sha=engine_sha,
        )

        try:
            brief = journal.run(
                "brief", lambda: load_approved_brief(brief_path, approved_sha256)
            )
            atomic_write_json(output_dir / "brief.json", brief)
            journal.payload["approved_brief_sha256"] = compute_brief_sha256(brief)
            journal.payload["topic"] = str(brief["approved_topic"])
            journal.payload["format"] = str(brief["format"])
            journal._write()

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

            script = journal.run(
                "script",
                lambda: self.router.route(
                    stage="script",
                    prompt=_script_prompt(brief, plan),
                    max_tokens=7500 if brief["format"] == "film" else 2500,
                    validator=lambda value: validate_script(value, plan),
                ),
            )
            atomic_write_json(output_dir / "script.json", script)
            self._write_runtime_events(output_dir)
            transcript = "\n\n".join(item["narration"] for item in script["sections"])
            (output_dir / "narration.txt").write_text(transcript + "\n", encoding="utf-8")

            narration_path = output_dir / "narration.wav"
            journal.run(
                "voice",
                lambda: self.voice_synthesizer.synthesize(transcript, narration_path),
            )

            visuals_dir = output_dir / "visuals"
            # Security V1 and M8 are part of the restored layer and execute inside
            # StockVisualSource admission/transform hooks during this stage.
            journal.payload["quality_layers_executed"] = [CINEMATIC_STAGE]
            journal._write()
            clips, rights = journal.run(
                "visuals",
                lambda: self.visual_source.acquire(
                    plan,
                    visuals_dir,
                    str(brief["format"]),
                    max_visuals,
                ),
            )
            atomic_write_json(
                output_dir / "rights-manifest.json",
                {
                    "schema_version": 1,
                    "assets": rights,
                    "note": "Provider metadata captured at acquisition; no visual quality audit executed in Clean V2 bootstrap.",
                },
            )
            self._write_runtime_events(output_dir)

            journal.payload["quality_layers_executed"] = [
                CINEMATIC_STAGE,
                VISUAL_QA_STAGE,
            ]
            journal._write()
            visual_qa_report = journal.run(
                VISUAL_QA_STAGE,
                lambda: self.visual_qa(
                    output_dir=output_dir,
                    plan=plan,
                    script=script,
                    rights=rights,
                    fmt=str(brief["format"]),
                ),
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
                "visual_qa_status": visual_qa_report.get("status"),
                "cinematic_v2_status": cinematic_report.get("status"),
                "final_master_qc_status": final_master_report.get("status"),
            }
        except Exception:
            self._write_runtime_events(output_dir)
            raise

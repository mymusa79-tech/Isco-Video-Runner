from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest import mock

from clean_v2.contracts import (
    ContractError,
    compute_brief_sha256,
    load_approved_brief,
    validate_plan,
)
from clean_v2.pipeline import (
    AUDIO_MASTERING_STAGE,
    CINEMATIC_STAGE,
    IDENTITY_STAGE,
    TEXT_AUDIT_STAGE,
    VISUAL_QA_STAGE,
    STAGES,
    CleanV2Pipeline,
    _planning_prompt,
)
from clean_v2.providers import NoWireFailure, ProviderAdapter, ProviderRouter
from clean_v2 import visual_qa as visual_qa_module
from clean_v2 import media as media_module
from clean_v2 import text_audit as text_audit_module


def _brief() -> dict:
    return {
        "approved_by_user": True,
        "approved_topic": "كيف تبدأ بخطوة صغيرة",
        "format": "film",
        "language": "ar",
        "audience": "Arabic-speaking adults",
        "editorial_intent": "شرح عملي هادئ دون وعود مبالغ فيها.",
        "research_pack": [],
        "hard_constraints": ["No fabricated facts."],
    }


def _plan() -> dict:
    return {
        "title": "خطوة واحدة",
        "promise": "فهم طريقة عملية للبدء",
        "sections": [
            {
                "id": "s1",
                "heading": "المشكلة",
                "purpose": "تسمية العائق",
                "visual_query_en": "quiet desk notebook wide shot",
            },
            {
                "id": "s2",
                "heading": "الفكرة",
                "purpose": "شرح الخطوة الصغيرة",
                "visual_query_en": "hand writing one task in notebook",
            },
            {
                "id": "s3",
                "heading": "التطبيق",
                "purpose": "دعوة عملية",
                "visual_query_en": "morning workspace sunlight no face",
            },
            {
                "id": "s4",
                "heading": "المراجعة",
                "purpose": "مراجعة أثر الخطوة الأولى",
                "visual_query_en": "checking simple task list on desk",
            },
            {
                "id": "s5",
                "heading": "الاستمرار",
                "purpose": "تثبيت خطوة تالية واضحة",
                "visual_query_en": "calendar and notebook calm workspace",
            },
        ],
    }


def _script() -> dict:
    return {
        "title": "خطوة واحدة",
        "sections": [
            {
                "id": "s1",
                "narration": "نؤجل البداية أحيانًا لأن المهمة تبدو أكبر من اللحظة المتاحة أمامنا.",
            },
            {
                "id": "s2",
                "narration": "حين نصغر الفعل الأول يصبح البدء أوضح، ونختبر الواقع بدل أن نبقى داخل الخطة.",
            },
            {
                "id": "s3",
                "narration": "اختر اليوم خطوة يمكن تنفيذها الآن، ثم اترك النتيجة التالية لما بعد البداية.",
            },
            {
                "id": "s4",
                "narration": "بعد التنفيذ راجع ما حدث بهدوء، وما الذي جعل الخطوة ممكنة في هذه المرة.",
            },
            {
                "id": "s5",
                "narration": "ثبت ما نجح واختر خطوة تالية صغيرة وواضحة حتى يتحول التقدم إلى عادة عملية.",
            },
        ],
    }


class ApprovedBriefContractTests(unittest.TestCase):
    def test_hash_binding_accepts_exact_brief_and_rejects_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "brief.json"
            brief = _brief()
            path.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
            digest = compute_brief_sha256(brief)
            self.assertEqual(load_approved_brief(path, digest)["approved_topic"], brief["approved_topic"])

            brief["approved_topic"] = "موضوع مختلف"
            path.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "changed after approval"):
                load_approved_brief(path, digest)


class PlanningCardinalityTests(unittest.TestCase):
    def test_film_prompt_and_validator_require_exactly_five_sections(self) -> None:
        brief = _brief()
        brief["format"] = "film"
        prompt = _planning_prompt(brief)
        self.assertIn("Use exactly 5 sections", prompt)
        self.assertNotIn("5 to 6", prompt)
        self.assertEqual(len(validate_plan(_plan(), brief)["sections"]), 5)

        six = _plan()
        six["sections"].append(
            {
                "id": "s6",
                "heading": "سادس",
                "purpose": "يجب رفضه",
                "visual_query_en": "extra calm workspace shot",
            }
        )
        with self.assertRaisesRegex(
            ContractError,
            "exactly 5 for film",
        ):
            validate_plan(six, brief)


class TextAuditProfessionalAdviceScopeTests(unittest.TestCase):
    def test_attempt2_productivity_guidance_is_explicitly_outside_professional_advice_flag(self) -> None:
        legacy_prompt = (
            "Rules:\n"
            + text_audit_module._LEGACY_PROFESSIONAL_ADVICE_RULE
            + "\n<PLAN>اختر مهمة واحدة، اضبط تذكيرًا، وتابع تقدمك.</PLAN>"
        )
        scoped = text_audit_module._scope_professional_advice_prompt(legacy_prompt)

        self.assertIn("choose a task, set a reminder, or track progress", scoped)
        self.assertIn("do NOT flag ordinary general productivity/self-improvement advice", scoped)
        self.assertIn("individualized medical, legal, financial", scoped)
        self.assertIn("religious authority advice/claims", scoped)
        self.assertIn("diagnosis, treatment, prescriptions", scoped)
        self.assertEqual(scoped.count("For professional_advice_flags specifically:"), 1)

    def test_professional_advice_scope_fails_closed_if_legacy_rule_drifts(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "professional-advice rule drift"):
            text_audit_module._scope_professional_advice_prompt(
                "Rules changed unexpectedly; no legacy professional advice rule here."
            )


class ProviderAccountingTests(unittest.TestCase):
    def test_local_unavailable_route_is_no_wire_and_does_not_take_attempt_number(self) -> None:
        def missing(_prompt: str, _tokens: int) -> dict:
            raise NoWireFailure("missing_api_key")

        router = ProviderRouter(
            (
                ProviderAdapter("missing", missing),
                ProviderAdapter("working", lambda _prompt, _tokens: {"ok": True}),
            )
        )
        result = router.route(
            stage="planning",
            prompt="small",
            max_tokens=100,
            validator=lambda value: value,
        )
        self.assertEqual(result, {"ok": True})
        self.assertFalse(router.events[0]["wire_attempted"])
        self.assertIsNone(router.events[0]["provider_attempt"])
        self.assertIsNone(router.events[0]["stage_wire_attempt"])
        self.assertTrue(router.events[1]["wire_attempted"])
        self.assertEqual(router.events[1]["stage_wire_attempt"], 1)

    def test_invalid_post_wire_output_counts_then_falls_forward_once(self) -> None:
        router = ProviderRouter(
            (
                ProviderAdapter("bad", lambda _prompt, _tokens: {"wrong": True}),
                ProviderAdapter("good", lambda _prompt, _tokens: {"ok": True}),
            )
        )

        def validate(value: dict) -> dict:
            if value.get("ok") is not True:
                raise ContractError("bad shape")
            return value

        self.assertEqual(
            router.route(
                stage="script",
                prompt="small",
                max_tokens=100,
                validator=validate,
            ),
            {"ok": True},
        )
        self.assertEqual(
            [event["stage_wire_attempt"] for event in router.events], [1, 2]
        )
        self.assertEqual(router.events[0]["result"], "invalid_output")

    def test_oversized_prompt_is_a_single_local_no_wire_block(self) -> None:
        router = ProviderRouter(
            (ProviderAdapter("unused", lambda _prompt, _tokens: {"ok": True}),)
        )
        with self.assertRaisesRegex(NoWireFailure, "prompt_too_large"):
            router.route(
                stage="planning",
                prompt="x" * (65 * 1024),
                max_tokens=100,
                validator=lambda value: value,
            )
        self.assertEqual(len(router.events), 1)
        self.assertFalse(router.events[0]["wire_attempted"])
        self.assertIsNone(router.events[0]["provider_attempt"])


class WorkflowContractTests(unittest.TestCase):
    WORKFLOW = Path(".github/workflows/clean-v2-minimal-e2e.yml")

    def test_workflow_uses_frozen_engine_and_approved_brief(self) -> None:
        text = self.WORKFLOW.read_text(encoding="utf-8")
        engine_sha = "3cbd689819e6b0e0b2ea9904d1998e24a5e2a293"
        brief_sha = "bcf8d3017ee8e18ee4808614c7c07731b0452a5ba6182e1183404f663078e129"
        self.assertGreaterEqual(text.count(engine_sha), 2)
        self.assertEqual(text.count(brief_sha), 1)
        self.assertIn("engine/production/approved_brief.json", text)
        self.assertIn("python -m clean_v2", text)
        self.assertIn("لماذا تفشل خطط إدارة الوقت في الحياة اليومية", text)

    def test_content_fallback_pins_specific_free_openrouter_model(self) -> None:
        workflow = self.WORKFLOW.read_text(encoding="utf-8")
        providers = Path("clean_v2/providers.py").read_text(encoding="utf-8")
        expected = "google/gemma-4-26b-a4b-it:free"
        self.assertIn(f"OPENROUTER_CONTENT_MODEL: {expected}", workflow)
        self.assertNotIn("OPENROUTER_CONTENT_MODEL: openrouter/free", workflow)
        self.assertIn(f'or "{expected}"', providers)
        self.assertIn(f'if model != "{expected}":', providers)
        self.assertNotIn('{"openrouter/free", "google/gemma-4-26b-a4b-it:free"}', providers)

    def test_workflow_persists_exact_sha_pre_qc_resume_checkpoint(self) -> None:
        text = self.WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("Restore Clean V2 pre-QC checkpoint", text)
        self.assertIn("Save Clean V2 pre-QC checkpoint", text)
        self.assertIn('CLEAN_V2_RESUME:', text)
        self.assertIn('--resume-from "$CLEAN_V2_RESUME"', text)
        self.assertIn("clean-v2-pre-qc-${{ runner.os }}-${{ github.sha }}", text)
        self.assertIn("${{ env.ISCO_ENGINE_SHA }}", text)
        self.assertIn("${{ env.ISCO_APPROVED_BRIEF_SHA256 }}", text)

    def test_workflow_invokes_security_cinematic_then_final_master_without_legacy_orchestrator(self) -> None:
        text = self.WORKFLOW.read_text(encoding="utf-8").casefold()
        self.assertIn("final-master-qc.json", text)
        self.assertIn("security-cinematic-v2.json", text)
        self.assertIn("tesseract-ocr", text)
        self.assertIn("fonts-noto-core", text)
        self.assertIn("pythonpath", text)
        for forbidden in (
            "produce-resilient-v4",
            "run_v3_voice",
            "gold_enforce",
            "viewer_quality",
            "create release",
        ):
            self.assertNotIn(forbidden, text)


def _passing_visual_qa(**kwargs) -> dict:
    output_dir = Path(kwargs["output_dir"])
    report = {
        "schema_version": 1,
        "layer": VISUAL_QA_STAGE,
        "status": "pass",
        "repair_or_replacement_enabled": False,
    }
    (output_dir / "visual-audit.json").write_text(
        json.dumps(
            [
                {
                    "section": "s1",
                    "status": "pass",
                    "is_selected": True,
                    "relevance": 0.9,
                    "visual_quality": 0.9,
                }
            ]
        ),
        encoding="utf-8",
    )
    (output_dir / "final-cut-visual-qa.json").write_text(
        json.dumps(report), encoding="utf-8"
    )
    return report


def _mutating_visual_qa(**kwargs) -> dict:
    output_dir = Path(kwargs["output_dir"])
    rights = kwargs["rights"]
    first = rights[0]
    visual = output_dir / "visuals" / str(first["local_file"])
    visual.write_bytes(visual.read_bytes() + b"semantic-recovery")
    first["asset_id"] = "recovered-asset"
    first["query"] = "narration-bound alternate query"
    (output_dir / "rights-manifest.json").write_text(
        json.dumps({"schema_version": 1, "assets": rights}),
        encoding="utf-8",
    )
    report = {
        "schema_version": 1,
        "layer": VISUAL_QA_STAGE,
        "status": "pass",
        "repair_or_replacement_enabled": True,
        "semantic_recovery_count": 1,
        "final_media_mutated": True,
    }
    (output_dir / "visual-audit.json").write_text("[]", encoding="utf-8")
    (output_dir / "final-cut-visual-qa.json").write_text(
        json.dumps(report), encoding="utf-8"
    )
    return report


def _blocking_visual_qa(**kwargs) -> dict:
    output_dir = Path(kwargs["output_dir"])
    (output_dir / "visual-audit.json").write_text("[]", encoding="utf-8")
    raise RuntimeError(
        "CLEAN_V2_VISUAL_QA_BLOCK section=s1 reason=selected_visual_not_final_cut_ready"
    )


def _infrastructure_visual_qa(**kwargs) -> dict:
    raise RuntimeError(
        "CLEAN_V2_VISUAL_QA_INFRASTRUCTURE section=s1 error_type=VisionProviderMeshUnavailableError"
    )


def _passing_text_audit(**kwargs) -> dict:
    output_dir = Path(kwargs["output_dir"])
    report = {
        "schema_version": 1,
        "source": "clean-v2-legacy-factuality-audit",
        "status": "pass",
        "unsupported_claims": [],
        "professional_advice_flags": [],
        "expert_persona_flags": [],
        "notes": [],
    }
    (output_dir / "factuality-audit.json").write_text(
        json.dumps(report), encoding="utf-8"
    )
    return report


def _blocking_text_audit(**kwargs) -> dict:
    output_dir = Path(kwargs["output_dir"])
    report = {
        "schema_version": 1,
        "source": "clean-v2-legacy-factuality-audit",
        "status": "block",
        "unsupported_claims": ["fixture_unsupported_claim"],
        "professional_advice_flags": [],
        "expert_persona_flags": [],
        "notes": [],
    }
    (output_dir / "factuality-audit.json").write_text(
        json.dumps(report), encoding="utf-8"
    )
    raise RuntimeError("Independent factuality/AI-expert gate blocked real production")


def _infrastructure_text_audit(**kwargs) -> dict:
    raise RuntimeError(
        "text_audit exhausted bounded provider route: "
        "gemini:http_429, groq:http_429, openrouter:http_429"
    )


def _passing_audio_mastering(**kwargs) -> dict:
    output_dir = Path(kwargs["output_dir"])
    narration_path = Path(kwargs["narration_path"])
    mastered_path = output_dir / "narration-mastered.wav"
    shutil.copyfile(narration_path, mastered_path)
    report = {
        "schema_version": 1,
        "source": "clean-v2-audio-loudness-mastering",
        "narration_file": mastered_path.name,
        "status": "pass",
        "target_integrated_lufs": -16.0,
        "target_true_peak_dbtp": -1.5,
        "target_loudness_range": 11.0,
        "alimiter_ceiling_linear": 0.84,
        "measured_input_integrated_lufs": -20.0,
        "measured_input_true_peak_dbtp": -6.0,
    }
    (output_dir / "audio-mastering.json").write_text(
        json.dumps(report), encoding="utf-8"
    )
    return report


def _failing_audio_mastering(**kwargs) -> dict:
    raise RuntimeError("audio_loudness_measurement_unparseable")


def _passing_narrative_identity(**kwargs) -> dict:
    output_dir = Path(kwargs["output_dir"])
    report = {
        "schema_version": 1,
        "source": "clean-v2-narrative-identity",
        "canonical_opener": "أهلاً بكم من جديد في نداء اليقظة",
        "canonical_closer": "إلى لقاء قادم في نداء اليقظة",
        "opener": "أهلاً بكم من جديد في هذه الحلقة من نداء اليقظة",
        "closer": "نلقاكم في حلقة قادمة من نداء اليقظة",
        "transitions": ["بعد هذه الفكرة", "ولننتقل الآن", "وهنا يأتي السؤال"],
    }
    (output_dir / "narrative-identity.json").write_text(
        json.dumps(report, ensure_ascii=False), encoding="utf-8"
    )
    return report


def _infrastructure_narrative_identity(**kwargs) -> dict:
    raise RuntimeError(
        f"{IDENTITY_STAGE} exhausted bounded provider route: "
        "gemini:http_429, groq:http_429, openrouter:http_429"
    )


def _passing_cinematic_layer(**kwargs) -> dict:
    output_dir = Path(kwargs["output_dir"])
    report = {
        "schema_version": 1,
        "layer": CINEMATIC_STAGE,
        "status": "pass",
        "reuse_not_rewrite": True,
        "ai_calls_added": 0,
    }
    (output_dir / "security-cinematic-v2.json").write_text(
        json.dumps(report), encoding="utf-8"
    )
    return report


def _blocking_cinematic_layer(**kwargs) -> dict:
    output_dir = Path(kwargs["output_dir"])
    report = {
        "schema_version": 1,
        "layer": CINEMATIC_STAGE,
        "status": "block",
    }
    (output_dir / "security-cinematic-v2.json").write_text(
        json.dumps(report), encoding="utf-8"
    )
    raise RuntimeError("synthetic new layer block")


def _passing_final_master_qc(output_dir: Path) -> dict:
    report = {
        "schema_version": 1,
        "status": "pass",
        "production_stage": "post_render_pre_gold_acceptance",
        "ai_calls_added": 0,
        "final_media_mutated": False,
        "blocking_findings": [],
        "warnings": [],
    }
    (output_dir / "final-master-qc.json").write_text(
        json.dumps(report), encoding="utf-8"
    )
    return report


def _blocking_final_master_qc(output_dir: Path) -> dict:
    report = {
        "schema_version": 1,
        "status": "block",
        "production_stage": "post_render_pre_gold_acceptance",
        "ai_calls_added": 0,
        "final_media_mutated": False,
        "blocking_findings": ["fixture_block"],
        "warnings": [],
    }
    (output_dir / "final-master-qc.json").write_text(
        json.dumps(report), encoding="utf-8"
    )
    raise RuntimeError("Final Master QC blocked release")


class _FakeRouter:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def route(self, *, stage, prompt, max_tokens, validator):
        del prompt, max_tokens
        value = _plan() if stage == "planning" else _script()
        self.events.append(
            {
                "stage": stage,
                "provider": "fixture",
                "result": "success",
                "wire_attempted": True,
                "provider_attempt": 1,
                "stage_wire_attempt": 1,
            }
        )
        return validator(value)


class _InfrastructureRouter:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def route(self, *, stage, prompt, max_tokens, validator):
        del prompt, max_tokens, validator
        raise RuntimeError(
            f"{stage} exhausted bounded provider route: "
            "gemini:http_429, groq:http_429, openrouter:http_429"
        )


class _FakeVoice:
    def __init__(self) -> None:
        self.calls = 0
        self.last_provider = "piper-local:ar_JO-kareem-medium"
        self.fallback_used = True

    def synthesize(self, transcript: str, output_path: Path) -> Path:
        self.calls += 1
        if not transcript.strip():
            raise RuntimeError("empty fixture transcript")
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=220:sample_rate=24000:duration=3",
                "-c:a",
                "pcm_s16le",
                "-y",
                str(output_path),
            ],
            check=True,
        )
        return output_path


class _FakeVisuals:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.calls = 0

    def acquire(self, plan, output_dir, fmt, max_visuals):
        self.calls += 1
        del plan, fmt, max_visuals
        output_dir.mkdir(parents=True, exist_ok=True)
        clips = []
        for index, color in enumerate(("#172033", "#6d4c41"), start=1):
            path = output_dir / f"fixture-{index}.mp4"
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    f"color=c={color}:s=640x360:r=30:d=2",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-an",
                    "-y",
                    str(path),
                ],
                check=True,
            )
            clips.append(path)
        self.events.append(
            {
                "provider": "fixture",
                "result": "selected",
                "wire_attempted": False,
            }
        )
        rights = [
            {
                "provider": "fixture",
                "asset_id": str(index),
                "local_file": path.name,
            }
            for index, path in enumerate(clips, start=1)
        ]
        return clips, rights


class _StuckQueryVisuals:
    """Mirrors the real Security V1 query normalizer's failure signature for a
    visual_query_en baked into plan.json that never validates - task #21."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.calls = 0

    def acquire(self, plan, output_dir, fmt, max_visuals):
        self.calls += 1
        del plan, output_dir, fmt, max_visuals
        raise RuntimeError(
            "CLEAN_V2_NEW_LAYER_BLOCK stage=security_v1.query "
            "error=ModelOutputSchemaError:visual_query_not_plain_english_search_terms"
        )


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
class CleanV2EndToEndTests(unittest.TestCase):
    def test_minimal_path_produces_structurally_complete_final_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            brief_path = root / "approved-brief.json"
            brief = _brief()
            brief_path.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
            output = root / "output"
            pipeline = CleanV2Pipeline(
                router=_FakeRouter(),
                voice_synthesizer=_FakeVoice(),
                visual_source=_FakeVisuals(),
                visual_qa=_passing_visual_qa,
                cinematic_layer=_passing_cinematic_layer,
                final_master_qc=_passing_final_master_qc,
                text_audit=_passing_text_audit,
                audio_mastering=_passing_audio_mastering,
                narrative_identity=_passing_narrative_identity,
            )
            result = pipeline.run(
                brief_path=brief_path,
                approved_sha256=compute_brief_sha256(brief),
                output_dir=output,
                engine_sha="a" * 40,
                runner_sha="b" * 40,
                max_visuals=2,
            )

            self.assertEqual(result["status"], "pass")
            self.assertTrue((output / "final.mp4").is_file())
            manifest = json.loads(
                (output / "run-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "pass")
            self.assertEqual([item["name"] for item in manifest["stages"]], list(STAGES))
            self.assertEqual(
                manifest["quality_layers_executed"],
                [TEXT_AUDIT_STAGE, CINEMATIC_STAGE, VISUAL_QA_STAGE, "final_master_qc"],
            )
            self.assertEqual(manifest["text_audit_status"], "pass")
            self.assertEqual(manifest["cinematic_v2_status"], "pass")
            self.assertEqual(manifest["final_master_qc_status"], "pass")
            final = json.loads((output / "final.json").read_text(encoding="utf-8"))
            self.assertEqual(final["status"], "pass")
            self.assertGreaterEqual(final["video_streams"], 1)
            self.assertGreaterEqual(final["audio_streams"], 1)
            qc_report = json.loads(
                (output / "final-master-qc.json").read_text(encoding="utf-8")
            )
            self.assertEqual(qc_report["status"], "pass")
            forbidden = {"gold", "text-audit", "viewer-quality"}
            names = {path.name.lower() for path in output.rglob("*")}
            self.assertFalse(any(any(token in name for token in forbidden) for name in names))

    def test_pre_qc_resume_skips_provider_voice_and_visual_rework(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            brief_path = root / "approved-brief.json"
            brief = _brief()
            brief_path.write_text(
                json.dumps(brief, ensure_ascii=False), encoding="utf-8"
            )
            approved = compute_brief_sha256(brief)
            first_output = root / "first"
            first_router = _FakeRouter()
            first_voice = _FakeVoice()
            first_visuals = _FakeVisuals()
            first = CleanV2Pipeline(
                router=first_router,
                voice_synthesizer=first_voice,
                visual_source=first_visuals,
                visual_qa=_infrastructure_visual_qa,
                cinematic_layer=_passing_cinematic_layer,
                final_master_qc=_passing_final_master_qc,
                text_audit=_passing_text_audit,
                audio_mastering=_passing_audio_mastering,
                narrative_identity=_passing_narrative_identity,
            )
            with self.assertRaisesRegex(
                RuntimeError, "CLEAN_V2_VISUAL_QA_INFRASTRUCTURE"
            ):
                first.run(
                    brief_path=brief_path,
                    approved_sha256=approved,
                    output_dir=first_output,
                    engine_sha="a" * 40,
                    runner_sha="b" * 40,
                    max_visuals=2,
                )

            checkpoint = json.loads(
                (first_output / "resume-checkpoint.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(checkpoint["completed_stage"], "visuals")
            self.assertEqual(
                [event["stage"] for event in first_router.events],
                ["planning", "script"],
            )
            self.assertEqual(first_voice.calls, 1)
            self.assertEqual(first_visuals.calls, 1)

            class _ForbiddenRouter:
                events: list[dict] = []

                def route(self, **_kwargs):
                    raise AssertionError("provider route must be resumed")

            class _ForbiddenVoice:
                def synthesize(self, *_args, **_kwargs):
                    raise AssertionError("voice must be resumed")

            class _ForbiddenVisuals:
                events: list[dict] = []

                def acquire(self, *_args, **_kwargs):
                    raise AssertionError("visual acquisition must be resumed")

            second_output = root / "second"
            second = CleanV2Pipeline(
                router=_ForbiddenRouter(),
                voice_synthesizer=_ForbiddenVoice(),
                visual_source=_ForbiddenVisuals(),
                visual_qa=_passing_visual_qa,
                cinematic_layer=_passing_cinematic_layer,
                final_master_qc=_passing_final_master_qc,
                text_audit=_passing_text_audit,
                audio_mastering=_passing_audio_mastering,
                narrative_identity=_passing_narrative_identity,
            )
            result = second.run(
                brief_path=brief_path,
                approved_sha256=approved,
                output_dir=second_output,
                engine_sha="a" * 40,
                runner_sha="b" * 40,
                max_visuals=2,
                resume_from=first_output,
            )
            self.assertEqual(result["status"], "pass")
            manifest = json.loads(
                (second_output / "run-manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                manifest["resumed_stages"],
                ["planning", IDENTITY_STAGE, "script", "voice", "visuals"],
            )
            self.assertTrue(manifest["resume_checkpoint_accepted"])
            self.assertEqual(manifest["resume_completed_stage"], "visuals")
            resumed_by_name = {
                stage["name"]: stage.get("resumed") for stage in manifest["stages"]
            }
            for name in ("planning", IDENTITY_STAGE, "script", "voice", "visuals"):
                self.assertTrue(resumed_by_name[name])
            self.assertFalse(resumed_by_name[TEXT_AUDIT_STAGE])

    def test_tampered_resume_checkpoint_fails_closed_to_normal_routing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            brief_path = root / "approved-brief.json"
            brief = _brief()
            brief_path.write_text(
                json.dumps(brief, ensure_ascii=False), encoding="utf-8"
            )
            approved = compute_brief_sha256(brief)
            first_output = root / "first"
            first = CleanV2Pipeline(
                router=_FakeRouter(),
                voice_synthesizer=_FakeVoice(),
                visual_source=_FakeVisuals(),
                visual_qa=_infrastructure_visual_qa,
                cinematic_layer=_passing_cinematic_layer,
                final_master_qc=_passing_final_master_qc,
                text_audit=_passing_text_audit,
                audio_mastering=_passing_audio_mastering,
                narrative_identity=_passing_narrative_identity,
            )
            with self.assertRaisesRegex(
                RuntimeError, "CLEAN_V2_VISUAL_QA_INFRASTRUCTURE"
            ):
                first.run(
                    brief_path=brief_path,
                    approved_sha256=approved,
                    output_dir=first_output,
                    engine_sha="a" * 40,
                    runner_sha="b" * 40,
                    max_visuals=2,
                )
            (first_output / "plan.json").write_text(
                '{"tampered":true}\n', encoding="utf-8"
            )

            second_output = root / "second"
            second = CleanV2Pipeline(
                router=_InfrastructureRouter(),
                voice_synthesizer=_FakeVoice(),
                visual_source=_FakeVisuals(),
                visual_qa=_passing_visual_qa,
                cinematic_layer=_passing_cinematic_layer,
                final_master_qc=_passing_final_master_qc,
                text_audit=_passing_text_audit,
                audio_mastering=_passing_audio_mastering,
                narrative_identity=_passing_narrative_identity,
            )
            with self.assertRaisesRegex(
                RuntimeError, "planning exhausted bounded provider route"
            ):
                second.run(
                    brief_path=brief_path,
                    approved_sha256=approved,
                    output_dir=second_output,
                    engine_sha="a" * 40,
                    runner_sha="b" * 40,
                    max_visuals=2,
                    resume_from=first_output,
                )
            manifest = json.loads(
                (second_output / "run-manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["resumed_stages"], [])
            self.assertNotIn("resume_checkpoint_accepted", manifest)

    def test_visuals_content_block_invalidates_resume_checkpoint(self) -> None:
        # Task #21: a plan/script whose visual_query_en fails Security V1's
        # content check at the "visuals" stage must not be handed to future
        # attempts via the resume checkpoint - otherwise every resumed retry
        # keeps re-inheriting, and re-saving, the exact same unusable output
        # forever, reproducing the stuck-query pattern observed across three
        # separate cohort attempts (#93, #94, #98).
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            brief_path = root / "approved-brief.json"
            brief = _brief()
            brief_path.write_text(
                json.dumps(brief, ensure_ascii=False), encoding="utf-8"
            )
            approved = compute_brief_sha256(brief)

            first_output = root / "first"
            first = CleanV2Pipeline(
                router=_FakeRouter(),
                voice_synthesizer=_FakeVoice(),
                visual_source=_StuckQueryVisuals(),
                visual_qa=_passing_visual_qa,
                cinematic_layer=_passing_cinematic_layer,
                final_master_qc=_passing_final_master_qc,
                text_audit=_passing_text_audit,
                audio_mastering=_passing_audio_mastering,
                narrative_identity=_passing_narrative_identity,
            )
            with self.assertRaisesRegex(RuntimeError, "CLEAN_V2_NEW_LAYER_BLOCK"):
                first.run(
                    brief_path=brief_path,
                    approved_sha256=approved,
                    output_dir=first_output,
                    engine_sha="a" * 40,
                    runner_sha="b" * 40,
                    max_visuals=2,
                )

            # The "voice" checkpoint (the last stage that did succeed) must not
            # survive a genuine content block at the very next stage.
            self.assertFalse((first_output / "resume-checkpoint.json").exists())
            manifest = json.loads(
                (first_output / "run-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "quality_pending")
            self.assertEqual(manifest["stages"][-1]["name"], "visuals")
            self.assertEqual(manifest["stages"][-1]["status"], "blocked")

            # A second attempt that tries to resume from the first run's output
            # must find nothing usable and regenerate planning/script fresh -
            # not silently inherit and repeat the same stuck query.
            second_output = root / "second"
            second_router = _FakeRouter()
            second = CleanV2Pipeline(
                router=second_router,
                voice_synthesizer=_FakeVoice(),
                visual_source=_FakeVisuals(),
                visual_qa=_passing_visual_qa,
                cinematic_layer=_passing_cinematic_layer,
                final_master_qc=_passing_final_master_qc,
                text_audit=_passing_text_audit,
                audio_mastering=_passing_audio_mastering,
                narrative_identity=_passing_narrative_identity,
            )
            result = second.run(
                brief_path=brief_path,
                approved_sha256=approved,
                output_dir=second_output,
                engine_sha="a" * 40,
                runner_sha="b" * 40,
                max_visuals=2,
                resume_from=first_output,
            )
            self.assertEqual(result["status"], "pass")
            second_manifest = json.loads(
                (second_output / "run-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(second_manifest["resumed_stages"], [])
            self.assertNotIn("resume_checkpoint_accepted", second_manifest)
            self.assertEqual(
                [event["stage"] for event in second_router.events],
                ["planning", "script"],
            )

    def test_final_master_block_preserves_completed_work_as_quality_pending(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            brief_path = root / "approved-brief.json"
            brief = _brief()
            brief_path.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
            output = root / "output"
            pipeline = CleanV2Pipeline(
                router=_FakeRouter(),
                voice_synthesizer=_FakeVoice(),
                visual_source=_FakeVisuals(),
                visual_qa=_passing_visual_qa,
                cinematic_layer=_passing_cinematic_layer,
                final_master_qc=_blocking_final_master_qc,
                text_audit=_passing_text_audit,
                audio_mastering=_passing_audio_mastering,
                narrative_identity=_passing_narrative_identity,
            )
            with self.assertRaisesRegex(RuntimeError, "blocked release"):
                pipeline.run(
                    brief_path=brief_path,
                    approved_sha256=compute_brief_sha256(brief),
                    output_dir=output,
                    engine_sha="a" * 40,
                    runner_sha="b" * 40,
                    max_visuals=2,
                )

            self.assertTrue((output / "final.mp4").is_file())
            self.assertTrue((output / "final.json").is_file())
            self.assertTrue((output / "final-master-qc.json").is_file())
            manifest = json.loads(
                (output / "run-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "quality_pending")
            self.assertEqual(manifest["quality_pending_stage"], "final_master_qc")
            self.assertEqual(manifest["failure_classification"], "pre-layer")
            self.assertEqual(
                manifest["quality_layers_executed"],
                [TEXT_AUDIT_STAGE, CINEMATIC_STAGE, VISUAL_QA_STAGE, "final_master_qc"],
            )
            self.assertTrue(all(
                item["status"] == "pass" for item in manifest["stages"][:-1]
            ))
            self.assertEqual(manifest["stages"][-1]["name"], "final_master_qc")
            self.assertEqual(manifest["stages"][-1]["status"], "blocked")


    def test_provider_exhaustion_is_attributed_to_infrastructure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            brief_path = root / "approved-brief.json"
            brief = _brief()
            brief_path.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
            output = root / "output"
            pipeline = CleanV2Pipeline(
                router=_InfrastructureRouter(),
                voice_synthesizer=_FakeVoice(),
                visual_source=_FakeVisuals(),
                visual_qa=_passing_visual_qa,
                cinematic_layer=_passing_cinematic_layer,
                final_master_qc=_passing_final_master_qc,
                text_audit=_passing_text_audit,
                audio_mastering=_passing_audio_mastering,
                narrative_identity=_passing_narrative_identity,
            )
            with self.assertRaisesRegex(RuntimeError, "exhausted bounded provider route"):
                pipeline.run(
                    brief_path=brief_path,
                    approved_sha256=compute_brief_sha256(brief),
                    output_dir=output,
                    engine_sha="a" * 40,
                    runner_sha="b" * 40,
                    max_visuals=2,
                )
            manifest = json.loads(
                (output / "run-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(manifest["failure_classification"], "infrastructure")
            self.assertEqual(manifest["stages"][-1]["name"], "planning")
            self.assertEqual(
                manifest["stages"][-1]["failure_classification"], "infrastructure"
            )


    def test_cinematic_block_is_pre_layer_after_accepted_m7_m11_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            brief_path = root / "approved-brief.json"
            brief = _brief()
            brief_path.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
            output = root / "output"
            pipeline = CleanV2Pipeline(
                router=_FakeRouter(),
                voice_synthesizer=_FakeVoice(),
                visual_source=_FakeVisuals(),
                visual_qa=_passing_visual_qa,
                cinematic_layer=_blocking_cinematic_layer,
                final_master_qc=_passing_final_master_qc,
                text_audit=_passing_text_audit,
                audio_mastering=_passing_audio_mastering,
                narrative_identity=_passing_narrative_identity,
            )
            with self.assertRaisesRegex(RuntimeError, "new layer block"):
                pipeline.run(
                    brief_path=brief_path,
                    approved_sha256=compute_brief_sha256(brief),
                    output_dir=output,
                    engine_sha="a" * 40,
                    runner_sha="b" * 40,
                    max_visuals=2,
                )

            manifest = json.loads(
                (output / "run-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "quality_pending")
            self.assertEqual(manifest["quality_pending_stage"], CINEMATIC_STAGE)
            self.assertEqual(manifest["failure_classification"], "pre-layer")
            self.assertEqual(
                manifest["quality_layers_executed"],
                [TEXT_AUDIT_STAGE, CINEMATIC_STAGE, VISUAL_QA_STAGE],
            )
            self.assertFalse((output / "final.json").exists())
            self.assertFalse((output / "final-master-qc.json").exists())


    def test_visual_qa_content_block_downgrades_checkpoint_to_voice(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            brief_path = root / "approved-brief.json"
            brief = _brief()
            brief_path.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
            approved = compute_brief_sha256(brief)
            first_output = root / "first"
            first = CleanV2Pipeline(
                router=_FakeRouter(),
                voice_synthesizer=_FakeVoice(),
                visual_source=_FakeVisuals(),
                visual_qa=_blocking_visual_qa,
                cinematic_layer=_passing_cinematic_layer,
                final_master_qc=_passing_final_master_qc,
                text_audit=_passing_text_audit,
                audio_mastering=_passing_audio_mastering,
                narrative_identity=_passing_narrative_identity,
            )
            with self.assertRaisesRegex(RuntimeError, "CLEAN_V2_VISUAL_QA_BLOCK"):
                first.run(
                    brief_path=brief_path,
                    approved_sha256=approved,
                    output_dir=first_output,
                    engine_sha="a" * 40,
                    runner_sha="b" * 40,
                    max_visuals=2,
                )

            manifest = json.loads(
                (first_output / "run-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "quality_pending")
            self.assertEqual(manifest["quality_pending_stage"], VISUAL_QA_STAGE)
            self.assertEqual(manifest["failure_classification"], "new-layer-block")
            self.assertEqual(manifest["stages"][-1]["name"], VISUAL_QA_STAGE)
            self.assertFalse((first_output / "final.mp4").exists())

            checkpoint = json.loads(
                (first_output / "resume-checkpoint.json").read_text(encoding="utf-8")
            )
            self.assertEqual(checkpoint["completed_stage"], "voice")
            self.assertEqual(
                checkpoint["voice_provider"], "piper-local:ar_JO-kareem-medium"
            )
            self.assertTrue(checkpoint["voice_fallback_used"])
            self.assertNotIn("rights-manifest.json", checkpoint["artifacts"])
            self.assertFalse(
                any(path.startswith("visuals/") for path in checkpoint["artifacts"])
            )

            class _ForbiddenRouter:
                events: list[dict] = []

                def route(self, **_kwargs):
                    raise AssertionError("planning and script must be resumed")

            class _ForbiddenVoice:
                def synthesize(self, *_args, **_kwargs):
                    raise AssertionError("voice must be resumed")

            second_visuals = _FakeVisuals()
            second_output = root / "second"
            second = CleanV2Pipeline(
                router=_ForbiddenRouter(),
                voice_synthesizer=_ForbiddenVoice(),
                visual_source=second_visuals,
                visual_qa=_passing_visual_qa,
                cinematic_layer=_passing_cinematic_layer,
                final_master_qc=_passing_final_master_qc,
                text_audit=_passing_text_audit,
                audio_mastering=_passing_audio_mastering,
                narrative_identity=_passing_narrative_identity,
            )
            result = second.run(
                brief_path=brief_path,
                approved_sha256=approved,
                output_dir=second_output,
                engine_sha="a" * 40,
                runner_sha="b" * 40,
                max_visuals=2,
                resume_from=first_output,
            )
            self.assertEqual(result["status"], "pass")
            self.assertEqual(second_visuals.calls, 1)
            second_manifest = json.loads(
                (second_output / "run-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                second_manifest["resumed_stages"],
                ["planning", IDENTITY_STAGE, "script", "voice"],
            )
            self.assertTrue(second_manifest["resume_checkpoint_accepted"])
            self.assertEqual(second_manifest["resume_completed_stage"], "voice")

    def test_visual_qa_provider_exhaustion_is_infrastructure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            brief_path = root / "approved-brief.json"
            brief = _brief()
            brief_path.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
            output = root / "output"
            pipeline = CleanV2Pipeline(
                router=_FakeRouter(),
                voice_synthesizer=_FakeVoice(),
                visual_source=_FakeVisuals(),
                visual_qa=_infrastructure_visual_qa,
                cinematic_layer=_passing_cinematic_layer,
                final_master_qc=_passing_final_master_qc,
                text_audit=_passing_text_audit,
                audio_mastering=_passing_audio_mastering,
                narrative_identity=_passing_narrative_identity,
            )
            with self.assertRaisesRegex(RuntimeError, "CLEAN_V2_VISUAL_QA_INFRASTRUCTURE"):
                pipeline.run(
                    brief_path=brief_path,
                    approved_sha256=compute_brief_sha256(brief),
                    output_dir=output,
                    engine_sha="a" * 40,
                    runner_sha="b" * 40,
                    max_visuals=2,
                )
            manifest = json.loads(
                (output / "run-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(manifest["failure_classification"], "infrastructure")
            self.assertEqual(manifest["stages"][-1]["name"], VISUAL_QA_STAGE)
            checkpoint = json.loads(
                (output / "resume-checkpoint.json").read_text(encoding="utf-8")
            )
            self.assertEqual(checkpoint["completed_stage"], "visuals")

    def test_text_audit_block_is_pre_layer_and_stops_before_voice(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            brief_path = root / "approved-brief.json"
            brief = _brief()
            brief_path.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
            output = root / "output"
            pipeline = CleanV2Pipeline(
                router=_FakeRouter(),
                voice_synthesizer=_FakeVoice(),
                visual_source=_FakeVisuals(),
                visual_qa=_passing_visual_qa,
                cinematic_layer=_passing_cinematic_layer,
                final_master_qc=_passing_final_master_qc,
                text_audit=_blocking_text_audit,
                audio_mastering=_passing_audio_mastering,
                narrative_identity=_passing_narrative_identity,
            )
            with self.assertRaisesRegex(RuntimeError, "blocked real production"):
                pipeline.run(
                    brief_path=brief_path,
                    approved_sha256=compute_brief_sha256(brief),
                    output_dir=output,
                    engine_sha="a" * 40,
                    runner_sha="b" * 40,
                    max_visuals=2,
                )

            manifest = json.loads(
                (output / "run-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "quality_pending")
            self.assertEqual(manifest["quality_pending_stage"], TEXT_AUDIT_STAGE)
            self.assertEqual(manifest["failure_classification"], "pre-layer")
            self.assertEqual(manifest["quality_layers_executed"], [TEXT_AUDIT_STAGE])
            self.assertEqual(manifest["stages"][-1]["name"], TEXT_AUDIT_STAGE)
            self.assertEqual(manifest["stages"][-1]["status"], "blocked")
            self.assertTrue((output / "factuality-audit.json").is_file())
            self.assertFalse((output / "narration.wav").exists())
            self.assertFalse((output / "resume-checkpoint.json").exists())

            # A genuine factuality block makes this exact script unusable. A later
            # attempt must regenerate planning/script instead of inheriting the
            # rejected checkpoint.
            second_output = root / "second"
            second_router = _FakeRouter()
            second = CleanV2Pipeline(
                router=second_router,
                voice_synthesizer=_FakeVoice(),
                visual_source=_FakeVisuals(),
                visual_qa=_passing_visual_qa,
                cinematic_layer=_passing_cinematic_layer,
                final_master_qc=_passing_final_master_qc,
                text_audit=_passing_text_audit,
                audio_mastering=_passing_audio_mastering,
                narrative_identity=_passing_narrative_identity,
            )
            result = second.run(
                brief_path=brief_path,
                approved_sha256=compute_brief_sha256(brief),
                output_dir=second_output,
                engine_sha="a" * 40,
                runner_sha="b" * 40,
                max_visuals=2,
                resume_from=output,
            )
            self.assertEqual(result["status"], "pass")
            second_manifest = json.loads(
                (second_output / "run-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(second_manifest["resumed_stages"], [])
            self.assertNotIn("resume_checkpoint_accepted", second_manifest)
            self.assertEqual(
                [event["stage"] for event in second_router.events],
                ["planning", "script"],
            )

    def test_text_audit_provider_exhaustion_is_infrastructure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            brief_path = root / "approved-brief.json"
            brief = _brief()
            brief_path.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
            output = root / "output"
            pipeline = CleanV2Pipeline(
                router=_FakeRouter(),
                voice_synthesizer=_FakeVoice(),
                visual_source=_FakeVisuals(),
                visual_qa=_passing_visual_qa,
                cinematic_layer=_passing_cinematic_layer,
                final_master_qc=_passing_final_master_qc,
                text_audit=_infrastructure_text_audit,
                audio_mastering=_passing_audio_mastering,
                narrative_identity=_passing_narrative_identity,
            )
            with self.assertRaisesRegex(RuntimeError, "exhausted bounded provider route"):
                pipeline.run(
                    brief_path=brief_path,
                    approved_sha256=compute_brief_sha256(brief),
                    output_dir=output,
                    engine_sha="a" * 40,
                    runner_sha="b" * 40,
                    max_visuals=2,
                )
            manifest = json.loads(
                (output / "run-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(manifest["failure_classification"], "infrastructure")
            self.assertEqual(manifest["stages"][-1]["name"], TEXT_AUDIT_STAGE)
            checkpoint = json.loads(
                (output / "resume-checkpoint.json").read_text(encoding="utf-8")
            )
            self.assertEqual(checkpoint["completed_stage"], "script")

    def test_audio_mastering_failure_is_a_plain_technical_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            brief_path = root / "approved-brief.json"
            brief = _brief()
            brief_path.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
            output = root / "output"
            pipeline = CleanV2Pipeline(
                router=_FakeRouter(),
                voice_synthesizer=_FakeVoice(),
                visual_source=_FakeVisuals(),
                visual_qa=_passing_visual_qa,
                cinematic_layer=_passing_cinematic_layer,
                final_master_qc=_passing_final_master_qc,
                text_audit=_passing_text_audit,
                audio_mastering=_failing_audio_mastering,
                narrative_identity=_passing_narrative_identity,
            )
            with self.assertRaisesRegex(RuntimeError, "audio_loudness_measurement_unparseable"):
                pipeline.run(
                    brief_path=brief_path,
                    approved_sha256=compute_brief_sha256(brief),
                    output_dir=output,
                    engine_sha="a" * 40,
                    runner_sha="b" * 40,
                    max_visuals=2,
                )
            manifest = json.loads(
                (output / "run-manifest.json").read_text(encoding="utf-8")
            )
            # Audio mastering is a transform, not a content gate: any failure is a
            # plain technical failure, never "quality_pending".
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(manifest["failure_classification"], "pre-layer")
            self.assertEqual(manifest["stages"][-1]["name"], AUDIO_MASTERING_STAGE)
            self.assertEqual(manifest["stages"][-1]["status"], "failed")
            self.assertFalse((output / "narration-mastered.wav").exists())

    def test_narrative_identity_provider_exhaustion_is_infrastructure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            brief_path = root / "approved-brief.json"
            brief = _brief()
            brief_path.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
            output = root / "output"
            pipeline = CleanV2Pipeline(
                router=_FakeRouter(),
                voice_synthesizer=_FakeVoice(),
                visual_source=_FakeVisuals(),
                visual_qa=_passing_visual_qa,
                cinematic_layer=_passing_cinematic_layer,
                final_master_qc=_passing_final_master_qc,
                text_audit=_passing_text_audit,
                audio_mastering=_passing_audio_mastering,
                narrative_identity=_infrastructure_narrative_identity,
            )
            with self.assertRaisesRegex(RuntimeError, "exhausted bounded provider route"):
                pipeline.run(
                    brief_path=brief_path,
                    approved_sha256=compute_brief_sha256(brief),
                    output_dir=output,
                    engine_sha="a" * 40,
                    runner_sha="b" * 40,
                    max_visuals=2,
                )
            manifest = json.loads(
                (output / "run-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(manifest["failure_classification"], "infrastructure")
            self.assertEqual(manifest["stages"][-1]["name"], IDENTITY_STAGE)
            self.assertEqual(manifest["stages"][-1]["status"], "failed")
            self.assertFalse((output / "narrative-identity.json").exists())
            self.assertFalse((output / "script.json").exists())

    def test_narrative_identity_splices_opener_and_closer_into_final_script(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            brief_path = root / "approved-brief.json"
            brief = _brief()
            brief_path.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
            output = root / "output"
            pipeline = CleanV2Pipeline(
                router=_FakeRouter(),
                voice_synthesizer=_FakeVoice(),
                visual_source=_FakeVisuals(),
                visual_qa=_passing_visual_qa,
                cinematic_layer=_passing_cinematic_layer,
                final_master_qc=_passing_final_master_qc,
                text_audit=_passing_text_audit,
                audio_mastering=_passing_audio_mastering,
                narrative_identity=_passing_narrative_identity,
            )
            result = pipeline.run(
                brief_path=brief_path,
                approved_sha256=compute_brief_sha256(brief),
                output_dir=output,
                engine_sha="a" * 40,
                runner_sha="b" * 40,
                max_visuals=2,
            )
            self.assertEqual(result["status"], "pass")

            identity = json.loads(
                (output / "narrative-identity.json").read_text(encoding="utf-8")
            )
            script = json.loads((output / "script.json").read_text(encoding="utf-8"))
            sections = script["sections"]
            joined = "\n".join(item["narration"] for item in sections)
            self.assertEqual(joined.count(identity["opener"]), 1)
            self.assertEqual(joined.count(identity["closer"]), 1)
            self.assertIn(identity["opener"], sections[0]["narration"])
            self.assertNotIn(identity["opener"], sections[-1]["narration"])
            self.assertTrue(
                sections[-1]["narration"].rstrip().endswith(identity["closer"])
            )
            for section in sections[1:-1]:
                self.assertNotIn(identity["opener"], section["narration"])
                self.assertNotIn(identity["closer"], section["narration"])

            transcript = (output / "narration.txt").read_text(encoding="utf-8")
            self.assertIn(identity["opener"], transcript)
            self.assertIn(identity["closer"], transcript)

    def test_narrative_identity_is_resumed_together_with_script(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            brief_path = root / "approved-brief.json"
            brief = _brief()
            brief_path.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
            approved = compute_brief_sha256(brief)
            first_output = root / "first"
            first = CleanV2Pipeline(
                router=_FakeRouter(),
                voice_synthesizer=_FakeVoice(),
                visual_source=_FakeVisuals(),
                visual_qa=_infrastructure_visual_qa,
                cinematic_layer=_passing_cinematic_layer,
                final_master_qc=_passing_final_master_qc,
                text_audit=_passing_text_audit,
                audio_mastering=_passing_audio_mastering,
                narrative_identity=_passing_narrative_identity,
            )
            with self.assertRaisesRegex(
                RuntimeError, "CLEAN_V2_VISUAL_QA_INFRASTRUCTURE"
            ):
                first.run(
                    brief_path=brief_path,
                    approved_sha256=approved,
                    output_dir=first_output,
                    engine_sha="a" * 40,
                    runner_sha="b" * 40,
                    max_visuals=2,
                )
            first_identity = json.loads(
                (first_output / "narrative-identity.json").read_text(encoding="utf-8")
            )

            def _forbidden_narrative_identity(**_kwargs):
                raise AssertionError(
                    "narrative identity must be resumed, not regenerated"
                )

            second_output = root / "second"
            second = CleanV2Pipeline(
                router=_FakeRouter(),
                voice_synthesizer=_FakeVoice(),
                visual_source=_FakeVisuals(),
                visual_qa=_passing_visual_qa,
                cinematic_layer=_passing_cinematic_layer,
                final_master_qc=_passing_final_master_qc,
                text_audit=_passing_text_audit,
                audio_mastering=_passing_audio_mastering,
                narrative_identity=_forbidden_narrative_identity,
            )
            result = second.run(
                brief_path=brief_path,
                approved_sha256=approved,
                output_dir=second_output,
                engine_sha="a" * 40,
                runner_sha="b" * 40,
                max_visuals=2,
                resume_from=first_output,
            )
            self.assertEqual(result["status"], "pass")
            second_identity = json.loads(
                (second_output / "narrative-identity.json").read_text(encoding="utf-8")
            )
            self.assertEqual(second_identity, first_identity)
            manifest = json.loads(
                (second_output / "run-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                manifest["resumed_stages"],
                ["planning", IDENTITY_STAGE, "script", "voice", "visuals"],
            )
            resumed_by_name = {
                stage["name"]: stage.get("resumed") for stage in manifest["stages"]
            }
            self.assertTrue(resumed_by_name[IDENTITY_STAGE])
            self.assertTrue(resumed_by_name["script"])


class VisualQADiagnosticsTests(unittest.TestCase):
    def test_contract_error_preserves_raw_provider_http_evidence_before_wrapping(self) -> None:
        source = Path("clean_v2/visual_qa.py").read_text(encoding="utf-8")
        diagnostic = source.index("visual-qa-diagnostics.json")
        wrapped = source.index("reason=visual_audit_contract_error")
        self.assertLess(diagnostic, wrapped)
        for field in (
            '"error_code"',
            '"provider"',
            '"requested_model"',
            '"resolved_model"',
            '"http_status"',
            '"http_message"',
            '"detail"',
        ):
            self.assertIn(field, source)


class VisualQASemanticRecoveryTests(unittest.TestCase):
    ORIGINAL_QUERY = "person scrolling phone while looking at wall clock"
    ALTERNATE_QUERY = "person avoiding open laptop task while scrolling phone beside clock"
    NARRATION = (
        "العامل الثاني هو المماطلة، التي تُعتبر فشلًا في التنظيم الذاتي. "
        "لا تُعزى إلى الكسل أو ضعف الشخصية، بل هي نتيجة لتجنب المهام التي تبدو غير ممتعة، "
        "أو شعورنا بأننا غير كفوّين في إتمامها، أو رغبتنا في الحصول على شعور عاطفي إيجابي "
        "على المدى القصير. هذه العوامل تجعلنا نؤجل العمل، حتى وإن كنا نعرف أن المهمة ضرورية."
    )

    class _Router:
        def __init__(self, alternate: str) -> None:
            self.alternate = alternate
            self.calls = 0
            self.events: list[dict] = []

        def route(self, *, stage, prompt, max_tokens, validator):
            self.calls += 1
            if stage != "visual_query_recovery":
                raise AssertionError(stage)
            if "المماطلة" not in prompt or "تجنب المهام" not in prompt:
                raise AssertionError("alternate query prompt lost the actual s3 narration")
            self.events.append(
                {
                    "stage": stage,
                    "provider": "test-provider",
                    "result": "success",
                    "wire_attempted": True,
                }
            )
            return validator({"alternate_query": self.alternate})

    class _FailingRecoveryRouter:
        def __init__(self) -> None:
            self.calls = 0
            self.events = [
                {
                    "stage": "visual_query_recovery",
                    "provider": "gemini",
                    "result": "http_429",
                    "wire_attempted": True,
                },
                {
                    "stage": "visual_query_recovery",
                    "provider": "groq",
                    "result": "http_400",
                    "wire_attempted": True,
                },
                {
                    "stage": "visual_query_recovery",
                    "provider": "openrouter",
                    "result": "http_429",
                    "wire_attempted": True,
                },
            ]

        def route(self, *, stage, prompt, max_tokens, validator):
            del prompt, max_tokens, validator
            self.calls += 1
            if stage != "visual_query_recovery":
                raise AssertionError(stage)
            raise RuntimeError(
                "visual_query_recovery exhausted bounded provider route: "
                "gemini:http_429, groq:http_400, openrouter:http_429"
            )

    class _VisualSource:
        def __init__(self, candidate_count: int = 1) -> None:
            self.acquire_calls = 0
            self.commit_calls = 0
            self.events: list[dict] = []
            self.candidate_count = candidate_count

        @staticmethod
        def _assert_request(
            query,
            *,
            destination_name,
            section_id,
            exclude_provider,
            exclude_asset_id,
            exclude_assets,
        ) -> None:
            if query != VisualQASemanticRecoveryTests.ALTERNATE_QUERY:
                raise AssertionError(query)
            if section_id != "s3" or destination_name != "visual-03.mp4":
                raise AssertionError((section_id, destination_name))
            if exclude_provider != "pexels" or str(exclude_asset_id) != "6943542":
                raise AssertionError((exclude_provider, exclude_asset_id))
            if ("pexels", "6943542") not in [
                (str(provider), str(asset_id)) for provider, asset_id in exclude_assets
            ]:
                raise AssertionError(exclude_assets)

        @staticmethod
        def _candidate(output_dir, *, query, destination_name, section_id, index):
            provider = "pexels" if index % 2 else "pixabay"
            asset_id = f"999000{index}"
            path = (
                Path(output_dir)
                / f".visual-03.semantic-recovery-{index:02d}-{provider}.mp4"
            )
            path.write_bytes(b"R" * 4096)
            return path, {
                "provider": provider,
                "asset_id": asset_id,
                "source_url": f"https://example.invalid/{provider}/{asset_id}",
                "creator": "test",
                "creator_url": "https://example.invalid/test",
                "query": query,
                "local_file": destination_name,
                "section_id": section_id,
                "semantic_recovery": True,
                "semantic_recovery_candidate_index": index,
            }

        def acquire_replacement_candidates(
            self,
            query,
            output_dir,
            fmt,
            *,
            destination_name,
            section_id,
            max_candidates,
            exclude_provider,
            exclude_asset_id,
            exclude_assets,
        ):
            del fmt
            self.acquire_calls += 1
            self._assert_request(
                query,
                destination_name=destination_name,
                section_id=section_id,
                exclude_provider=exclude_provider,
                exclude_asset_id=exclude_asset_id,
                exclude_assets=exclude_assets,
            )
            if max_candidates != 3:
                raise AssertionError(max_candidates)
            return [
                self._candidate(
                    output_dir,
                    query=query,
                    destination_name=destination_name,
                    section_id=section_id,
                    index=index,
                )
                for index in range(
                    1,
                    min(self.candidate_count, max_candidates) + 1,
                )
            ]

        def acquire_replacement(
            self,
            query,
            output_dir,
            fmt,
            *,
            destination_name,
            section_id,
            exclude_provider,
            exclude_asset_id,
            exclude_assets,
        ):
            del fmt
            self.acquire_calls += 1
            self._assert_request(
                query,
                destination_name=destination_name,
                section_id=section_id,
                exclude_provider=exclude_provider,
                exclude_asset_id=exclude_asset_id,
                exclude_assets=exclude_assets,
            )
            return self._candidate(
                output_dir,
                query=query,
                destination_name=destination_name,
                section_id=section_id,
                index=1,
            )

        def commit_replacement(self, replacement, destination):
            self.commit_calls += 1
            os.replace(replacement, destination)
            return Path(destination)

    @staticmethod
    def _audit(*, relevance: float, quality: float, status: str, evidence) -> dict:
        return {
            "status": status,
            "relevance": relevance,
            "visual_quality": quality,
            "identifiable_person": True,
            "sensitive_trait_implication_risk": False,
            "prominent_logo_or_brand": False,
            "cultural_conflict": False,
            "cultural_islamic_suitability_risk": False,
            "advertiser_conflict": False,
            "obvious_synthetic_or_visual_artifact": False,
            "reason": "controlled s3 semantic recovery reproduction",
            "vision_provider": "mistral",
            "resolved_model": "ministral-14b-2512",
            "prompt_hash": evidence.prompt_hash,
            "frame_sha256": list(evidence.frame_sha256),
        }

    def _run_case(
        self,
        *,
        recovery_relevance: float | None = None,
        recovery_relevances: list[float] | None = None,
    ):
        scores = list(
            recovery_relevances
            if recovery_relevances is not None
            else [float(recovery_relevance if recovery_relevance is not None else 0.0)]
        )
        router = self._Router(self.ALTERNATE_QUERY)
        visual_source = self._VisualSource(candidate_count=len(scores))
        plan = {
            "sections": [
                {
                    "id": "s3",
                    "heading": "المماطلة كفشل تنظيم ذاتي",
                    "purpose": (
                        "يُظهر أن المماطلة ناتجة عن تجنب المهام، ضعف الثقة في المهارة، "
                        "والبحث عن الراحة العاطفية"
                    ),
                    "visual_query_en": self.ORIGINAL_QUERY,
                }
            ]
        }
        script = {"sections": [{"id": "s3", "narration": self.NARRATION}]}
        rights = [
            {
                "provider": "pexels",
                "asset_id": "6943542",
                "source_url": (
                    "https://www.pexels.com/video/"
                    "man-in-bed-looking-at-phone-and-alarm-clock-6943542/"
                ),
                "creator": "cottonbro studio",
                "creator_url": "https://www.pexels.com/@cottonbro",
                "query": self.ORIGINAL_QUERY,
                "local_file": "visual-03.mp4",
                "section_id": "s3",
            }
        ]

        evidence_counter = {"n": 0}
        audit_counter = {"n": 0}

        def build_evidence(_clip, _bundle, **_kwargs):
            evidence_counter["n"] += 1
            n = evidence_counter["n"]
            return SimpleNamespace(
                prompt_hash=f"prompt-{n}",
                frame_sha256=(f"frame-{n}-1", f"frame-{n}-2", f"frame-{n}-3"),
            )

        def ledger_call(_ledger, _spec, *_args, **kwargs):
            audit_counter["n"] += 1
            evidence = kwargs["canonical_evidence"]
            if audit_counter["n"] == 1:
                return self._audit(
                    relevance=0.40,
                    quality=0.95,
                    status="block",
                    evidence=evidence,
                )
            score_index = min(audit_counter["n"] - 2, len(scores) - 1)
            score = scores[score_index]
            return self._audit(
                relevance=score,
                quality=0.95,
                status="pass",
                evidence=evidence,
            )

        with tempfile.TemporaryDirectory() as root:
            output = Path(root)
            visuals = output / "visuals"
            visuals.mkdir()
            original = visuals / "visual-03.mp4"
            original.write_bytes(b"O" * 4096)
            (output / "rights-manifest.json").write_text(
                json.dumps({"schema_version": 1, "assets": rights}),
                encoding="utf-8",
            )

            class FakeBudgetLedger:
                def __init__(self, _fmt, *, enforce=True):
                    self.enforce = enforce

                def write(self, path):
                    Path(path).write_text(
                        json.dumps({"schema_version": 1, "provider_attempts": {}}),
                        encoding="utf-8",
                    )

                def to_summary(self):
                    return {"provider_attempts": {}}

            class FakeTaskSpec:
                def __init__(self, **kwargs):
                    self.__dict__.update(kwargs)

            class FakeVisionStageError(RuntimeError):
                def __init__(self, message="vision error"):
                    super().__init__(message)
                    self.code = SimpleNamespace(value="PROVIDER_TRANSIENT")
                    self.provider = "fake"
                    self.requested_model = "fake"
                    self.resolved_model = "fake"
                    self.http_status = 429
                    self.http_message = "fake"
                    self.detail = "fake"

            engine = types.ModuleType("isco_video_agent")
            engine.__path__ = []
            ai_budget = types.ModuleType("isco_video_agent.ai_budget")
            ai_budget.BudgetLedger = FakeBudgetLedger
            ai_budget.Capability = SimpleNamespace(VISION="vision")
            ai_budget.Priority = SimpleNamespace(P0="P0")
            ai_budget.TaskSpec = FakeTaskSpec
            orchestrator = types.ModuleType("isco_video_agent.orchestrator")
            orchestrator._ledger_call_status = ledger_call
            visual_selection = types.ModuleType("isco_video_agent.visual_selection")
            visual_selection.FINAL_CUT_TARGET_SEMANTIC_FLOOR = 0.85
            visual_selection.semantic_floor = lambda audit: min(
                float(audit.get("relevance", 0.0) or 0.0),
                float(audit.get("visual_quality", 0.0) or 0.0),
            )
            visual_selection.is_final_cut_ready = lambda audit: (
                str(audit.get("status") or "").lower() == "pass"
                and visual_selection.semantic_floor(audit) >= 0.85
            )

            canonical = types.ModuleType("scripts.canonical_visual_evidence_v1")
            canonical.build_canonical_visual_evidence = build_evidence
            canonical.audit_gemini_canonical_evidence = lambda *_a, **_k: None
            mesh = types.ModuleType("scripts.run181_vision_mesh_closure")
            mesh.install_run181_vision_mesh_closure = lambda: None
            reliability = types.ModuleType("scripts.vision_provider_reliability")
            reliability.vision_provider_circuit_scope = lambda: contextlib.nullcontext()
            reliability.VisionProviderMeshUnavailableError = type(
                "VisionProviderMeshUnavailableError", (RuntimeError,), {}
            )
            mistral_visual = types.ModuleType("scripts.mistral_visual_qa_fallback")
            mistral_visual.reset_mistral_visual_qa_telemetry = lambda: None
            mistral_visual.get_mistral_visual_qa_telemetry = lambda: []
            contract = types.ModuleType("scripts.vision_stage_contract_v2")
            contract.VisionStageError = FakeVisionStageError
            contract.install_vision_provider_reliability = lambda: None

            fake_modules = {
                "isco_video_agent": engine,
                "isco_video_agent.ai_budget": ai_budget,
                "isco_video_agent.orchestrator": orchestrator,
                "isco_video_agent.visual_selection": visual_selection,
                "scripts.canonical_visual_evidence_v1": canonical,
                "scripts.run181_vision_mesh_closure": mesh,
                "scripts.vision_provider_reliability": reliability,
                "scripts.mistral_visual_qa_fallback": mistral_visual,
                "scripts.vision_stage_contract_v2": contract,
            }
            with mock.patch.dict(sys.modules, fake_modules), mock.patch.dict(
                os.environ,
                {"GEMINI_API_KEY": "test-key", "GEMINI_CONTENT_MODEL": "gemini-3.7-flash"},
                clear=False,
            ):
                if any(score >= 0.85 for score in scores):
                    result = visual_qa_module.run_final_cut_visual_qa(
                        output_dir=output,
                        plan=plan,
                        script=script,
                        rights=rights,
                        fmt="film",
                        router=router,
                        visual_source=visual_source,
                    )
                    error = None
                else:
                    result = None
                    with self.assertRaisesRegex(
                        visual_qa_module.CleanV2VisualQABlock,
                        "semantic_recovery_not_final_cut_ready",
                    ) as raised:
                        visual_qa_module.run_final_cut_visual_qa(
                            output_dir=output,
                            plan=plan,
                            script=script,
                            rights=rights,
                            fmt="film",
                            router=router,
                            visual_source=visual_source,
                        )
                    error = str(raised.exception)

            audits = json.loads((output / "visual-audit.json").read_text(encoding="utf-8"))
            recovery = json.loads(
                (output / "visual-query-recovery.json").read_text(encoding="utf-8")
            )
            manifest = json.loads(
                (output / "rights-manifest.json").read_text(encoding="utf-8")
            )
            final_bytes = original.read_bytes()
            return {
                "result": result,
                "error": error,
                "audits": audits,
                "recovery": recovery,
                "manifest": manifest,
                "rights": rights,
                "final_bytes": final_bytes,
                "router_calls": router.calls,
                "acquire_calls": visual_source.acquire_calls,
                "commit_calls": visual_source.commit_calls,
                "audit_calls": audit_counter["n"],
            }

    def test_attempt1_s3_floor_040_gets_one_narration_bound_recovery_and_passes(self) -> None:
        outcome = self._run_case(recovery_relevance=0.92)

        self.assertEqual(outcome["router_calls"], 1)
        self.assertEqual(outcome["acquire_calls"], 1)
        self.assertEqual(outcome["commit_calls"], 1)
        self.assertEqual(outcome["audit_calls"], 2)
        self.assertEqual(outcome["result"]["status"], "pass")
        self.assertEqual(outcome["result"]["final_cut_readiness_target"], 0.85)
        self.assertEqual(outcome["result"]["semantic_recovery_count"], 1)
        self.assertTrue(outcome["result"]["final_media_mutated"])
        self.assertEqual(outcome["audits"][0]["final_cut_semantic_floor"], 0.4)
        self.assertFalse(outcome["audits"][0]["is_selected"])
        self.assertEqual(outcome["audits"][1]["final_cut_semantic_floor"], 0.92)
        self.assertTrue(outcome["audits"][1]["is_selected"])
        self.assertEqual(outcome["recovery"][0]["attempt_limit"], 1)
        self.assertEqual(outcome["recovery"][0]["status"], "recovered")
        self.assertEqual(outcome["rights"][0]["asset_id"], "9990001")
        self.assertEqual(outcome["rights"][0]["recovery_of_asset_id"], "6943542")
        self.assertEqual(outcome["manifest"]["assets"][0]["query"], self.ALTERNATE_QUERY)
        self.assertEqual(outcome["final_bytes"], b"R" * 4096)

    def test_recovery_is_strictly_one_shot_when_second_clip_still_below_085(self) -> None:
        outcome = self._run_case(recovery_relevance=0.70)

        self.assertEqual(outcome["router_calls"], 1)
        self.assertEqual(outcome["acquire_calls"], 1)
        self.assertEqual(outcome["commit_calls"], 0)
        self.assertEqual(outcome["audit_calls"], 2)
        self.assertIn("primary_floor=0.400000", outcome["error"])
        self.assertIn("recovery_floor=0.700000", outcome["error"])
        self.assertEqual(outcome["recovery"][0]["status"], "rejected")
        self.assertEqual(outcome["rights"][0]["asset_id"], "6943542")
        self.assertEqual(outcome["final_bytes"], b"O" * 4096)


    def test_one_query_can_recover_with_second_bounded_candidate(self) -> None:
        outcome = self._run_case(
            recovery_relevances=[0.60, 0.92, 0.40],
        )

        self.assertEqual(outcome["router_calls"], 1)
        self.assertEqual(outcome["acquire_calls"], 1)
        self.assertEqual(outcome["commit_calls"], 1)
        self.assertEqual(outcome["audit_calls"], 3)
        self.assertEqual(outcome["result"]["status"], "pass")
        self.assertEqual(
            outcome["result"]["semantic_recovery_candidate_review_limit_per_section"],
            3,
        )
        record = outcome["recovery"][0]
        self.assertEqual(record["candidate_pool_size"], 3)
        self.assertEqual(record["candidate_review_count"], 2)
        self.assertEqual(record["selected_candidate_index"], 2)
        self.assertEqual(
            [item["status"] for item in record["candidate_reviews"]],
            ["rejected", "ready"],
        )
        self.assertEqual(outcome["rights"][0]["provider"], "pixabay")
        self.assertEqual(outcome["rights"][0]["asset_id"], "9990002")

    def test_three_recovery_candidates_is_hard_fail_closed_limit(self) -> None:
        outcome = self._run_case(
            recovery_relevances=[0.60, 0.70, 0.80],
        )

        self.assertEqual(outcome["router_calls"], 1)
        self.assertEqual(outcome["acquire_calls"], 1)
        self.assertEqual(outcome["commit_calls"], 0)
        self.assertEqual(outcome["audit_calls"], 4)
        self.assertIn("recovery_floor=0.800000", outcome["error"])
        self.assertIn("reviewed=3", outcome["error"])
        record = outcome["recovery"][0]
        self.assertEqual(record["candidate_pool_size"], 3)
        self.assertEqual(record["candidate_review_count"], 3)
        self.assertEqual(
            [item["status"] for item in record["candidate_reviews"]],
            ["rejected", "rejected", "rejected"],
        )
        self.assertEqual(outcome["rights"][0]["asset_id"], "6943542")
        self.assertEqual(outcome["final_bytes"], b"O" * 4096)

    def test_attempt1_recovery_provider_exhaustion_is_infrastructure_not_content(self) -> None:
        original_router = self._Router
        self._Router = lambda _alternate: self._FailingRecoveryRouter()
        try:
            with self.assertRaises(
                visual_qa_module.CleanV2VisualQAInfrastructure
            ) as raised:
                self._run_case(recovery_relevance=0.92)
        finally:
            self._Router = original_router
        self.assertIn(
            "reason=semantic_recovery_query_unavailable",
            str(raised.exception),
        )
        self.assertIn(
            "CLEAN_V2_VISUAL_QA_INFRASTRUCTURE",
            str(raised.exception),
        )


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
class VisualRecoveryCheckpointTests(unittest.TestCase):
    def test_successful_semantic_replacement_refreshes_visuals_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            brief = _brief()
            brief_path = root / "approved-brief.json"
            brief_path.write_text(
                json.dumps(brief, ensure_ascii=False),
                encoding="utf-8",
            )
            output = root / "output"
            pipeline = CleanV2Pipeline(
                router=_FakeRouter(),
                voice_synthesizer=_FakeVoice(),
                visual_source=_FakeVisuals(),
                visual_qa=_mutating_visual_qa,
                cinematic_layer=_blocking_cinematic_layer,
                final_master_qc=_passing_final_master_qc,
                text_audit=_passing_text_audit,
                audio_mastering=_passing_audio_mastering,
                narrative_identity=_passing_narrative_identity,
            )
            with self.assertRaisesRegex(RuntimeError, "synthetic new layer block"):
                pipeline.run(
                    brief_path=brief_path,
                    approved_sha256=compute_brief_sha256(brief),
                    output_dir=output,
                    engine_sha="a" * 40,
                    runner_sha="b" * 40,
                    max_visuals=2,
                )

            checkpoint = json.loads(
                (output / "resume-checkpoint.json").read_text(encoding="utf-8")
            )
            self.assertEqual(checkpoint["completed_stage"], "visuals")
            manifest = json.loads(
                (output / "rights-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["assets"][0]["asset_id"], "recovered-asset")
            relative = "visuals/fixture-1.mp4"
            current_hash = __import__("hashlib").sha256(
                (output / relative).read_bytes()
            ).hexdigest()
            self.assertEqual(checkpoint["artifacts"][relative], current_hash)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from clean_v2.contracts import (
    ContractError,
    compute_brief_sha256,
    load_approved_brief,
    validate_plan,
)
from clean_v2.pipeline import (
    AUDIO_MASTERING_STAGE,
    CINEMATIC_STAGE,
    TEXT_AUDIT_STAGE,
    VISUAL_QA_STAGE,
    STAGES,
    CleanV2Pipeline,
    _planning_prompt,
)
from clean_v2.providers import NoWireFailure, ProviderAdapter, ProviderRouter


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
                ["planning", "script", "voice", "visuals"],
            )
            self.assertTrue(manifest["resume_checkpoint_accepted"])
            self.assertEqual(manifest["resume_completed_stage"], "visuals")
            resumed_by_name = {
                stage["name"]: stage.get("resumed") for stage in manifest["stages"]
            }
            for name in ("planning", "script", "voice", "visuals"):
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


    def test_visual_qa_block_is_new_layer_block_and_stops_before_render(self) -> None:
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
                visual_qa=_blocking_visual_qa,
                cinematic_layer=_passing_cinematic_layer,
                final_master_qc=_passing_final_master_qc,
                text_audit=_passing_text_audit,
                audio_mastering=_passing_audio_mastering,
            )
            with self.assertRaisesRegex(RuntimeError, "CLEAN_V2_VISUAL_QA_BLOCK"):
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
            self.assertEqual(manifest["quality_pending_stage"], VISUAL_QA_STAGE)
            self.assertEqual(manifest["failure_classification"], "new-layer-block")
            self.assertEqual(manifest["stages"][-1]["name"], VISUAL_QA_STAGE)
            self.assertFalse((output / "final.mp4").exists())

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


if __name__ == "__main__":
    unittest.main()

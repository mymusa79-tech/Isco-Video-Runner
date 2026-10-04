from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from clean_v2.pipeline import (
    CleanV2Pipeline,
    IDENTITY_STAGE,
    VISUAL_BIND_STAGE,
    _Journal,
    _bind_writer_visual_story_with_recovery,
    _validate_plan_with_visual_world_recovery,
    _validate_resumed_visual_story,
)
from clean_v2.visual_story import (
    CHANNEL_VISUAL_IDENTITY,
    VisualFamilyRepeatError,
    VisualWorldIdentityError,
    require_channel_visual_world,
)
from scripts import telegram_clean_v2_control as control
from scripts.research_relevance_filter import market_sample_relevance


class _ExactWriterRouter:
    def __init__(self) -> None:
        self.events = [
            {
                "stage": "script",
                "provider": "mistral",
                "result": "success",
                "wire_attempted": True,
            }
        ]
        self.exact_calls: list[tuple[str, str]] = []

    def route_exact_provider(
        self,
        *,
        provider_name: str,
        stage: str,
        prompt: str,
        max_tokens: int,
        validator,
    ):
        self.exact_calls.append((provider_name, stage))
        return validator(
            {"alternate_query": "hands opening doorway toward quiet workspace"}
        )


class Run55PipelineIntegrityTests(unittest.TestCase):
    def test_script_checkpoint_stays_after_binding_and_post_script_persistence(self) -> None:
        source = inspect.getsource(CleanV2Pipeline.run)
        script_start = source.index('script = journal.run(')
        checkpoint = source.index('completed_stage="script"', script_start)
        pre_checkpoint = source[script_start:checkpoint]

        binding = pre_checkpoint.index("VISUAL_BIND_STAGE")
        brand = pre_checkpoint.index("_apply_brand_signature(")
        cta = pre_checkpoint.index("bind_contextual_cta_to_script(")
        script_write = pre_checkpoint.index(
            'atomic_write_json(output_dir / "script.json", script)'
        )
        narration_write = pre_checkpoint.index(
            '(output_dir / "narration.txt").write_text('
        )

        self.assertLess(binding, brand)
        self.assertLess(brand, cta)
        self.assertLess(cta, script_write)
        self.assertLess(script_write, narration_write)
        self.assertNotIn('completed_stage="script"', pre_checkpoint)

    def test_visual_binding_failure_marks_manifest_failed(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "run-manifest.json"
            journal = _Journal(path, runner_sha="runner", engine_sha="engine")
            journal.run("brief", lambda: {})
            journal.run("planning", lambda: {})
            journal.run(IDENTITY_STAGE, lambda: {})
            journal.run("script", lambda: {})
            with self.assertRaisesRegex(ValueError, "run55 visual bind"):
                journal.run(
                    VISUAL_BIND_STAGE,
                    lambda: (_ for _ in ()).throw(ValueError("run55 visual bind")),
                )

            manifest = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "failed")
            self.assertIsNotNone(manifest["finished_at"])
            self.assertEqual(manifest["stages"][-1]["name"], VISUAL_BIND_STAGE)
            self.assertEqual(manifest["stages"][-1]["status"], "failed")

    def test_visual_family_recovery_uses_same_writer_provider_and_is_bounded(self) -> None:
        router = _ExactWriterRouter()
        error = VisualFamilyRepeatError(
            "writer visual binding repeats visual family too often: stationery",
            family="stationery",
            beat_id="b3",
            section_id="s2",
            query="hand writing in notebook",
        )
        story = {
            "beats": [
                {
                    "id": "b3",
                    "section_id": "s2",
                    "meaning_target": "show a different concrete next step",
                    "semantic_must_have": ["one visible next step"],
                    "shot_intent": "hand writing in notebook",
                    "stock_query_en": "hand writing in notebook",
                }
            ]
        }

        with tempfile.TemporaryDirectory() as root, mock.patch(
            "clean_v2.pipeline._bind_writer_visual_story",
            side_effect=[error, {"beats": []}],
        ):
            bound = _bind_writer_visual_story_with_recovery(
                router=router,
                output_dir=Path(root),
                brief={},
                plan={},
                script={},
                visual_story=story,
            )

        self.assertEqual(bound, {"beats": []})
        self.assertEqual(
            router.exact_calls,
            [("mistral", "visual_query_recovery")],
        )
        retry_events = [
            event for event in router.events
            if event.get("reason") == "visual_family_repeat"
        ]
        self.assertEqual(len(retry_events), 1)
        self.assertEqual(retry_events[0]["rejected_family"], "stationery")
        self.assertEqual(retry_events[0]["recovery_attempt"], 1)

    def test_visual_family_recovery_never_exceeds_two_extra_writer_calls(self) -> None:
        router = _ExactWriterRouter()
        error = VisualFamilyRepeatError(
            "writer visual binding repeats visual family too often: stationery",
            family="stationery",
            beat_id="b3",
            section_id="s2",
            query="hand writing in notebook",
        )
        story = {
            "beats": [
                {
                    "id": "b3",
                    "section_id": "s2",
                    "meaning_target": "show a different concrete next step",
                    "semantic_must_have": ["one visible next step"],
                    "shot_intent": "hand writing in notebook",
                    "stock_query_en": "hand writing in notebook",
                }
            ]
        }

        with tempfile.TemporaryDirectory() as root, mock.patch(
            "clean_v2.pipeline._bind_writer_visual_story",
            side_effect=[error, error, error],
        ):
            with self.assertRaises(VisualFamilyRepeatError):
                _bind_writer_visual_story_with_recovery(
                    router=router,
                    output_dir=Path(root),
                    brief={},
                    plan={},
                    script={},
                    visual_story=story,
                )

        self.assertEqual(len(router.exact_calls), 2)
        self.assertTrue(
            all(provider == "mistral" for provider, _stage in router.exact_calls)
        )

    def test_visual_world_requires_dark_identity_and_gold_accent(self) -> None:
        accepted = require_channel_visual_world(
            "Deep blue and charcoal cinematic shadows with a restrained golden accent."
        )
        self.assertIn("charcoal", accepted)

        accepted_ar = require_channel_visual_world(
            "عمق أزرق داكن وفحمي مع لمسة ذهبية خفيفة وتباين سينمائي."
        )
        self.assertIn("ذهبية", accepted_ar)

        with self.assertRaises(VisualWorldIdentityError):
            require_channel_visual_world(
                "بيئة منزلية هادئة ذات إضاءة طبيعية دافئة وألوان محايدة."
            )

    def test_resumed_visual_world_uses_host_identity_fallback_without_ai(self) -> None:
        router = type("Router", (), {"events": []})()
        value = {
            "visual_world": "warm quiet home with natural light",
            "story_arc": {
                "beginning": "friction",
                "transformation": "turn",
                "arrival": "progress",
            },
            "retention_thread": {
                "hook_tension": "tension",
                "payoff_answer": "answer",
                "visual_motif": "motif",
            },
            "beats": [
                {
                    "id": "b1",
                    "section_id": "s1",
                    "viewer_intent": "يفهم المشاهد الفكرة",
                    "meaning_target": "one visible task state",
                    "semantic_must_have": ["one visible task object"],
                    "semantic_should_avoid": [],
                    "shot_intent": "closed task object beside doorway",
                    "role": "hook",
                    "stock_query_en": "closed task object beside doorway",
                    "display_text_ar": "بداية واضحة",
                    "source_preference": "stock_motion",
                    "shot_role": "action",
                    "environment_family": "home",
                    "hold_reason": "hook_progression",
                    "pause_intent": "micro",
                    "audio_energy": "steady",
                },
                {
                    "id": "b2",
                    "section_id": "s1",
                    "viewer_intent": "يرى المشاهد النتيجة المكتسبة",
                    "meaning_target": "one visible completed task state",
                    "semantic_must_have": ["one visible completed task object"],
                    "semantic_should_avoid": [],
                    "shot_intent": "completed task object beside open doorway",
                    "role": "payoff",
                    "stock_query_en": "completed task object beside open doorway",
                    "display_text_ar": "نتيجة واضحة",
                    "source_preference": "stock_motion",
                    "shot_role": "payoff",
                    "environment_family": "home",
                    "hold_reason": "payoff_landing",
                    "pause_intent": "ending",
                    "audio_energy": "resolve",
                },
            ],
        }
        plan = {
            "sections": [{
                "id": "s1",
                "purpose": "يفهم المشاهد الفكرة",
                "visual_query_en": "closed task object beside doorway",
            }],
        }

        repaired = _validate_resumed_visual_story(
            value,
            plan,
            router=router,
        )

        self.assertEqual(repaired["visual_world"], CHANNEL_VISUAL_IDENTITY)
        self.assertEqual(
            [event.get("reason") for event in router.events],
            ["visual_world_identity_resume_fallback"],
        )

    def test_visual_world_falls_back_on_first_host_owned_identity_rejection(self) -> None:
        router = type("Router", (), {"events": []})()
        state: dict[str, int] = {}
        value = {
            "visual_story": {
                "visual_world": "warm quiet home with natural light",
            }
        }
        brief = {"format": "short"}
        error = VisualWorldIdentityError(
            "visual_world_identity_missing navy/dark-blue/charcoal,gold/golden-accent"
        )

        with mock.patch(
            "clean_v2.pipeline._validate_plan_for_brief",
            side_effect=[
                error,
                {"visual_story": {"visual_world": CHANNEL_VISUAL_IDENTITY}},
            ],
        ):
            plan = _validate_plan_with_visual_world_recovery(
                value,
                brief,
                router=router,
                state=state,
            )

        self.assertEqual(
            plan["visual_story"]["visual_world"],
            CHANNEL_VISUAL_IDENTITY,
        )
        fallback_events = [
            event for event in router.events
            if event.get("reason") == "visual_world_identity_fallback"
        ]
        self.assertEqual(len(fallback_events), 1)
        self.assertEqual(fallback_events[0]["identity_rejections"], 1)
        self.assertFalse(fallback_events[0]["wire_attempted"])

    def test_research_relevance_rejects_run55_hygiene_false_positive(self) -> None:
        relevant, overlap = market_sample_relevance(
            "قوة 1% تغيير حياتك",
            {
                "title": "قاعدة 1% تغير حياتك بخطوة صغيرة كل يوم",
                "description": "تغيير بسيط يصنع فرقًا مع الوقت",
            },
        )
        self.assertTrue(relevant)
        self.assertGreaterEqual(len(overlap), 2)

        unrelated, overlap = market_sample_relevance(
            "قوة 1% تغيير حياتك",
            {
                "title": "النظافة حلوة كتير تعلم النظافة اليومية للأطفال",
                "description": "أغنية تعليمية للأطفال عن غسل اليدين والأسنان",
            },
        )
        self.assertFalse(unrelated)
        self.assertEqual(overlap, [])

    def test_market_evidence_filters_unrelated_youtube_samples_before_velocity_sort(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        videos = [
            {
                "id": "relevant",
                "snippet": {
                    "title": "قاعدة 1% تغير حياتك بخطوة صغيرة كل يوم",
                    "description": "تغيير بسيط يصنع فرقًا مع الوقت",
                    "publishedAt": now,
                    "channelTitle": "وعي",
                },
                "statistics": {"viewCount": "1000"},
            },
            {
                "id": "hygiene",
                "snippet": {
                    "title": "النظافة حلوة كتير تعلم النظافة اليومية للأطفال",
                    "description": "أغنية للأطفال عن غسل اليدين والأسنان",
                    "publishedAt": now,
                    "channelTitle": "أطفال",
                },
                "statistics": {"viewCount": "999999"},
            },
        ]

        with mock.patch.object(control, "youtube_search", return_value=videos):
            _score, evidence = control.market_evidence("قوة 1% تغيير حياتك")

        self.assertEqual(evidence["sample_count"], 1)
        self.assertEqual(evidence["top_samples"][0]["video_id"], "relevant")
        self.assertNotEqual(evidence["top_samples"][0]["video_id"], "hygiene")


if __name__ == "__main__":
    unittest.main()

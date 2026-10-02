from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clean_v2.contextual_cta import (
    apply_contextual_cta_overlay,
    bind_contextual_cta_to_script,
)
from clean_v2.contracts import ContractError, validate_plan


def _brief(fmt: str = "film") -> dict:
    return {
        "approved_topic": "لماذا تفشل خطط إدارة الوقت في الحياة اليومية",
        "pillar": "personal_development",
        "format": fmt,
    }


def _plan(cta: str) -> dict:
    return {
        "title": "إدارة الوقت في الحياة الحقيقية",
        "promise": "فهم لماذا تنهار الخطة عند أول احتكاك باليوم الحقيقي",
        "cta": cta,
        "sections": [
            {
                "id": f"s{index}",
                "heading": f"قسم {index}",
                "purpose": f"تقدم جديد في الفكرة {index}",
                "visual_query_en": f"daily planning routine detail {index}",
            }
            for index in range(1, 6)
        ],
    }


def _script() -> dict:
    return {
        "title": "إدارة الوقت في الحياة الحقيقية",
        "sections": [
            {
                "id": f"s{index}",
                "narration": (
                    f"هذا القسم رقم {index} يضيف طبقة جديدة ومحددة إلى الفكرة "
                    "ويشرحها بلغة عربية طبيعية وواضحة للمشاهد."
                ),
            }
            for index in range(1, 6)
        ],
    }


def _write_measured_film_cta_timeline(
    root: Path,
    *,
    start: float = 50.0,
    end: float = 54.0,
) -> None:
    (root / "timeline-first.json").write_text(
        json.dumps(
            {
                "audio_units": [
                    {"section_id": "s1", "role": "hook", "start": 0.0, "end": 5.0},
                    {"section_id": "s1", "role": "prayer", "start": 6.0, "end": 9.0},
                    {"section_id": "s1", "role": "channel_identity", "start": 9.0, "end": 14.0},
                    {"section_id": "s2", "role": "topic", "start": 20.0, "end": 40.0},
                    {"section_id": "s3", "role": "cta_topic", "start": start, "end": end},
                    {"section_id": "s3", "role": "topic", "start": end, "end": 60.0},
                    {"section_id": "s4", "role": "topic", "start": 60.0, "end": 78.0},
                    {"section_id": "s5", "role": "outro", "start": 88.0, "end": 92.0},
                ],
                "section_events": [
                    {"section_id": "s1", "start": 0.0, "end": 20.0},
                    {"section_id": "s2", "start": 20.0, "end": 40.0},
                    {"section_id": "s3", "start": 40.0, "end": 60.0},
                    {"section_id": "s4", "start": 60.0, "end": 80.0},
                    {"section_id": "s5", "start": 80.0, "end": 100.0},
                ],
                "identity_events": [
                    {"kind": "hook", "start": 0.0, "end": 5.0},
                    {"kind": "intro", "start": 5.0, "end": 6.0},
                    {"kind": "prayer", "start": 6.0, "end": 9.0},
                    {"kind": "channel_identity", "start": 9.0, "end": 14.0},
                    {"kind": "outro", "start": 88.0, "end": 92.0},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


class CleanV2ContextualCtaTests(unittest.TestCase):
    def test_film_plan_requires_contextual_cta(self) -> None:
        plan = _plan("")
        with self.assertRaisesRegex(ContractError, "contextual cta"):
            validate_plan(plan, _brief())

    def test_comment_cta_is_spoken_once_mid_late_with_zero_provider_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = _script()
            authored = "ما أكثر شيء يكسر خطتك خلال اليوم؟ اكتب تجربتك في التعليقات."
            report = bind_contextual_cta_to_script(
                output_dir=root,
                brief=_brief(),
                plan=_plan(authored),
                script=script,
            )
            self.assertEqual(report["mode"], "comment")
            self.assertEqual(report["anchor_section_id"], "s3")
            self.assertEqual(report["provider_calls_added"], 0)
            self.assertEqual(report["rules"]["provider_calls"], 0)
            self.assertFalse(report["visual_only"])
            self.assertEqual(report["spoken_text"], authored)
            joined = " ".join(item["narration"] for item in script["sections"])
            self.assertEqual(joined.count(authored), 1)
            self.assertNotIn(authored, script["sections"][0]["narration"])
            self.assertNotIn(authored, script["sections"][-1]["narration"])

            # Idempotent if the binding function is called again on the same script.
            bind_contextual_cta_to_script(
                output_dir=root,
                brief=_brief(),
                plan=_plan(authored),
                script=script,
            )
            joined_again = " ".join(item["narration"] for item in script["sections"])
            self.assertEqual(joined_again.count(authored), 1)

    def test_bundled_actions_are_rejected_without_script_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = _script()
            before = json.dumps(script, ensure_ascii=False, sort_keys=True)
            report = bind_contextual_cta_to_script(
                output_dir=root,
                brief=_brief(),
                plan=_plan("اشترك في القناة واضغط إعجابًا واكتب تعليقك."),
                script=script,
            )
            self.assertEqual(report["mode"], "none")
            self.assertEqual(report["reason"], "bundled_actions_rejected")
            self.assertEqual(
                json.dumps(script, ensure_ascii=False, sort_keys=True),
                before,
            )

    def test_like_is_spoken_and_not_visual_only_for_film(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = _script()
            authored = "إذا أضافت لك هذه الفكرة شيئًا، يكفيني إعجابك."
            report = bind_contextual_cta_to_script(
                output_dir=root,
                brief=_brief(),
                plan=_plan(authored),
                script=script,
            )
            self.assertEqual(report["mode"], "like")
            self.assertFalse(report["visual_only"])
            self.assertEqual(report["spoken_text"], authored)
            self.assertEqual(
                " ".join(item["narration"] for item in script["sections"]).count(authored),
                1,
            )

    def test_short_remains_without_spoken_social_cta(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = {
                "title": "شورت",
                "sections": [
                    {"id": "s1", "narration": "لماذا نتردد حين تكثر الخيارات؟"},
                    {"id": "s2", "narration": "كل مقارنة جديدة تستهلك جزءًا من انتباهنا."},
                    {"id": "s3", "narration": "الوضوح يأتي من معيار محدد. اختر معيارًا واحدًا الآن."},
                ],
            }
            before = json.dumps(script, ensure_ascii=False, sort_keys=True)
            plan = _plan("")
            plan["sections"] = plan["sections"][:3]
            report = bind_contextual_cta_to_script(
                output_dir=root,
                brief=_brief("short"),
                plan=plan,
                script=script,
            )
            self.assertEqual(report["mode"], "none")
            self.assertTrue(report["visual_only"])
            self.assertEqual(report["spoken_text"], "")
            self.assertEqual(json.dumps(script, ensure_ascii=False, sort_keys=True), before)

    def test_podcast_spoken_cta_is_inserted_inside_charon_b_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            authored = "إذا وجدت هذا النوع من الحوار مفيدًا، اشترك لتكمل الرحلة معنا."
            plan = {
                "title": "خارج النص",
                "promise": "فهم أعمق",
                "cta": authored,
                "sections": [
                    {"id": "s1", "heading": "سؤال", "purpose": "فتح التوتر", "visual_query_en": "hands paused over choices"},
                    {"id": "s2", "heading": "جواب", "purpose": "تعميق الجواب", "visual_query_en": "one option separated from several"},
                ],
            }
            script = {
                "title": "خارج النص",
                "sections": [
                    {"id": "s1", "narration": "A: لماذا نتردد؟ B: لأن كل خيار يضيف مقارنة جديدة."},
                    {
                        "id": "s2",
                        "narration": (
                            "A: وماذا يتغير حين نحدد معيارًا؟ "
                            "B: تقل المقارنات التي لا تخدم القرار. ويصبح الحسم أوضح."
                        ),
                    },
                ],
            }
            report = bind_contextual_cta_to_script(
                output_dir=root,
                brief=_brief("podcast"),
                plan=plan,
                script=script,
            )
            self.assertEqual(report["mode"], "subscribe")
            self.assertEqual(report["anchor_section_id"], "s2")
            narration = script["sections"][1]["narration"]
            self.assertEqual(narration.count(authored), 1)
            self.assertIn("B: تقل المقارنات", narration)
            self.assertIn(authored, narration)
            # CTA stays inside the final B turn; it never becomes an A question.
            self.assertNotIn(f"A: {authored}", narration)

    def test_overlay_uses_measured_timeline_section_spans_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = _script()
            bind_contextual_cta_to_script(
                output_dir=root,
                brief=_brief(),
                plan=_plan("ما أكثر شيء يكسر خطتك خلال اليوم؟ اكتب تجربتك في التعليقات."),
                script=script,
            )
            _write_measured_film_cta_timeline(root, start=44.0, end=48.0)
            final_path = root / "final.mp4"
            final_path.write_bytes(b"original-video")
            narration = root / "narration-mastered.wav"
            narration.write_bytes(b"audio")

            with mock.patch("clean_v2.media.probe_duration", return_value=100.0):
                report = apply_contextual_cta_overlay(
                    output_dir=root,
                    final_path=final_path,
                    narration_path=narration,
                    script=script,
                )

            self.assertEqual(report["section_duration_source"], "timeline-first")
            self.assertEqual(report["cta_schedule_source"], "measured-cta-topic-unit")
            self.assertEqual(report["schedule"]["start_seconds"], 44.0)
            self.assertEqual(report["schedule"]["end_seconds"], 48.0)
            self.assertEqual(report["schedule"]["anchor_section_id"], "s3")

    def test_longform_visual_cta_stays_inside_topic_role_not_outro(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            authored = "إذا وجدت هذا النوع من الحوار مفيدًا، اشترك لتكمل الرحلة معنا."
            plan = {
                "title": "خارج النص",
                "promise": "فهم أعمق",
                "cta": authored,
                "sections": [
                    {"id": "s1", "heading": "سؤال", "purpose": "فتح التوتر", "visual_query_en": "hands paused over choices"},
                    {"id": "s2", "heading": "جواب", "purpose": "تعميق الجواب", "visual_query_en": "one option separated from several"},
                ],
            }
            script = {
                "title": "خارج النص",
                "sections": [
                    {"id": "s1", "narration": "A: لماذا نتردد؟ B: لأن كل خيار يضيف مقارنة جديدة."},
                    {
                        "id": "s2",
                        "narration": (
                            "A: وماذا يتغير حين نحدد معيارًا؟ "
                            "B: تقل المقارنات التي لا تخدم القرار. ويصبح الحسم أوضح."
                        ),
                    },
                ],
            }
            bind_contextual_cta_to_script(
                output_dir=root,
                brief=_brief("podcast"),
                plan=plan,
                script=script,
            )
            (root / "timeline-first.json").write_text(
                json.dumps(
                    {
                        "audio_units": [
                            {"section_id": "s1", "role": "hook", "start": 0.0, "end": 5.0},
                            {"section_id": "s1", "role": "prayer", "start": 6.0, "end": 9.0},
                            {"section_id": "s1", "role": "topic", "start": 10.0, "end": 28.0},
                            {"section_id": "s2", "role": "cta_topic", "start": 46.0, "end": 50.0},
                            {"section_id": "s2", "role": "topic", "start": 50.0, "end": 70.0},
                            {"section_id": "s2", "role": "outro", "start": 70.0, "end": 78.0},
                        ],
                        "section_events": [
                            {"section_id": "s1", "start": 0.0, "end": 28.0},
                            {"section_id": "s2", "start": 28.0, "end": 78.0},
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            final_path = root / "final.mp4"
            final_path.write_bytes(b"original-video")
            narration = root / "narration-mastered.wav"
            narration.write_bytes(b"audio")

            with mock.patch("clean_v2.media.probe_duration", return_value=90.0):
                report = apply_contextual_cta_overlay(
                    output_dir=root,
                    final_path=final_path,
                    narration_path=narration,
                    script=script,
                )

            self.assertEqual(report["cta_schedule_source"], "measured-cta-topic-unit")
            schedule = report["schedule"]
            self.assertEqual(schedule["start_seconds"], 46.0)
            self.assertEqual(schedule["end_seconds"], 50.0)
            self.assertIn("outro", report["forbidden_regions"])
            self.assertIn("prayer", report["forbidden_regions"])

    def test_spoken_longform_cta_fails_closed_without_measured_cta_topic_unit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = _script()
            bind_contextual_cta_to_script(
                output_dir=root,
                brief=_brief(),
                plan=_plan("اشترك لتكمل الرحلة معنا."),
                script=script,
            )
            (root / "timeline-first.json").write_text(
                json.dumps(
                    {
                        "audio_units": [
                            {"section_id": "s1", "role": "hook", "start": 0.0, "end": 5.0},
                            {"section_id": "s3", "role": "topic", "start": 40.0, "end": 55.0},
                            {"section_id": "s5", "role": "outro", "start": 80.0, "end": 88.0},
                        ],
                        "section_events": [
                            {"section_id": f"s{index}", "start": (index - 1) * 20.0, "end": index * 20.0}
                            for index in range(1, 6)
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            final_path = root / "final.mp4"
            final_path.write_bytes(b"original-video")
            narration = root / "narration-mastered.wav"
            narration.write_bytes(b"audio")
            with mock.patch("clean_v2.media.probe_duration", return_value=100.0):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "must have one measured topic-only voice unit",
                ):
                    apply_contextual_cta_overlay(
                        output_dir=root,
                        final_path=final_path,
                        narration_path=narration,
                        script=script,
                    )

    def test_overlay_respects_first_30_and_final_12_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = _script()
            bind_contextual_cta_to_script(
                output_dir=root,
                brief=_brief(),
                plan=_plan("اشترك لتكمل الرحلة معنا."),
                script=script,
            )
            _write_measured_film_cta_timeline(root)
            final_path = root / "final.mp4"
            original = b"original-video"
            final_path.write_bytes(original)
            narration = root / "narration-mastered.wav"
            narration.write_bytes(b"audio")

            with mock.patch("clean_v2.media.probe_duration", return_value=100.0):
                report = apply_contextual_cta_overlay(
                    output_dir=root,
                    final_path=final_path,
                    narration_path=narration,
                    script=script,
                )

            schedule = report["schedule"]
            self.assertGreaterEqual(schedule["start_seconds"], 30.5)
            self.assertLessEqual(schedule["end_seconds"], 88.0)
            self.assertEqual(
                report["render_status"],
                "delegated_to_approved_visual_assets",
            )
            self.assertEqual(report["visual_owner"], "clean_v2.visual_cta")
            self.assertEqual(report["provider_calls_added"], 0)
            self.assertEqual(final_path.read_bytes(), original)

    def test_legacy_cta_renderer_is_not_used_after_asset_migration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = _script()
            bind_contextual_cta_to_script(
                output_dir=root,
                brief=_brief(),
                plan=_plan("اشترك لتكمل الرحلة معنا."),
                script=script,
            )
            _write_measured_film_cta_timeline(root)
            final_path = root / "final.mp4"
            original = b"original-video"
            final_path.write_bytes(original)
            narration = root / "narration-mastered.wav"
            narration.write_bytes(b"audio")

            with mock.patch("clean_v2.media.probe_duration", return_value=100.0), mock.patch(
                "clean_v2.contextual_cta.render_cta_overlay"
            ) as legacy_render:
                report = apply_contextual_cta_overlay(
                    output_dir=root,
                    final_path=final_path,
                    narration_path=narration,
                    script=script,
                )

            legacy_render.assert_not_called()
            self.assertEqual(
                report["render_status"],
                "delegated_to_approved_visual_assets",
            )
            self.assertEqual(final_path.read_bytes(), original)
            self.assertEqual(report["provider_calls_added"], 0)

    def test_moment_never_gets_cta(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = {
                "title": "لحظة",
                "sections": [{"id": "s1", "narration": "نص قصير لكنه مكتمل وواضح للمشاهد."}],
            }
            report = bind_contextual_cta_to_script(
                output_dir=root,
                brief=_brief("moment"),
                plan={
                    "title": "لحظة",
                    "promise": "وعد",
                    "cta": "اشترك لتكمل الرحلة معنا",
                    "sections": [
                        {
                            "id": "s1",
                            "heading": "لحظة",
                            "purpose": "فكرة",
                            "visual_query_en": "quiet coffee cup",
                        }
                    ],
                },
                script=script,
            )
            self.assertEqual(report["mode"], "none")
            self.assertEqual(report["reason"], "moment_no_cta")


if __name__ == "__main__":
    unittest.main()

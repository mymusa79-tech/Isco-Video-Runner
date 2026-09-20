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


class CleanV2ContextualCtaTests(unittest.TestCase):
    def test_film_plan_requires_contextual_cta(self) -> None:
        plan = _plan("")
        with self.assertRaisesRegex(ContractError, "contextual cta"):
            validate_plan(plan, _brief())

    def test_comment_cta_is_spoken_once_mid_late_with_zero_provider_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = _script()
            report = bind_contextual_cta_to_script(
                output_dir=root,
                brief=_brief(),
                plan=_plan("ما أكثر شيء يكسر خطتك خلال اليوم؟ اكتب تجربتك في التعليقات."),
                script=script,
            )

            self.assertEqual(report["mode"], "comment")
            self.assertEqual(report["anchor_section_id"], "s3")
            self.assertEqual(report["provider_calls_added"], 0)
            self.assertEqual(report["rules"]["provider_calls"], 0)
            spoken = report["spoken_text"]
            joined = "\n".join(item["narration"] for item in script["sections"])
            self.assertEqual(joined.count(spoken), 1)
            self.assertNotIn(spoken, script["sections"][0]["narration"])
            self.assertNotIn(spoken, script["sections"][-1]["narration"])

            # Idempotence: the legacy binding must never duplicate the spoken CTA.
            bind_contextual_cta_to_script(
                output_dir=root,
                brief=_brief(),
                plan=_plan("ما أكثر شيء يكسر خطتك خلال اليوم؟ اكتب تجربتك في التعليقات."),
                script=script,
            )
            joined_again = "\n".join(item["narration"] for item in script["sections"])
            self.assertEqual(joined_again.count(spoken), 1)

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

    def test_like_is_visual_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = _script()
            before = json.dumps(script, ensure_ascii=False, sort_keys=True)
            report = bind_contextual_cta_to_script(
                output_dir=root,
                brief=_brief(),
                plan=_plan("إذا أضافت لك الفكرة شيئًا، يكفيني إعجابك."),
                script=script,
            )
            self.assertEqual(report["mode"], "like")
            self.assertTrue(report["visual_only"])
            self.assertEqual(report["spoken_text"], "")
            self.assertEqual(
                json.dumps(script, ensure_ascii=False, sort_keys=True),
                before,
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
            final_path = root / "final.mp4"
            final_path.write_bytes(b"original-video")
            narration = root / "narration-mastered.wav"
            narration.write_bytes(b"audio")
            captured = {}

            def fake_render(src, binding, schedule, dest):
                captured["binding"] = binding
                captured["schedule"] = schedule
                shutil.copy2(src, dest)
                with dest.open("ab") as handle:
                    handle.write(b"-cta")
                return dest

            with mock.patch("clean_v2.media.probe_duration", return_value=100.0), mock.patch(
                "clean_v2.contextual_cta.render_cta_overlay",
                side_effect=fake_render,
            ):
                report = apply_contextual_cta_overlay(
                    output_dir=root,
                    final_path=final_path,
                    narration_path=narration,
                    script=script,
                )

            schedule = captured["schedule"]
            self.assertGreaterEqual(schedule.start_seconds, 30.5)
            self.assertLessEqual(schedule.end_seconds, 88.0)
            self.assertEqual(report["render_status"], "applied")
            self.assertEqual(report["provider_calls_added"], 0)
            self.assertTrue(final_path.read_bytes().endswith(b"-cta"))

    def test_overlay_render_failure_falls_back_to_original_video(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = _script()
            bind_contextual_cta_to_script(
                output_dir=root,
                brief=_brief(),
                plan=_plan("اشترك لتكمل الرحلة معنا."),
                script=script,
            )
            final_path = root / "final.mp4"
            original = b"original-video"
            final_path.write_bytes(original)
            narration = root / "narration-mastered.wav"
            narration.write_bytes(b"audio")

            with mock.patch("clean_v2.media.probe_duration", return_value=100.0), mock.patch(
                "clean_v2.contextual_cta.render_cta_overlay",
                side_effect=RuntimeError("ffmpeg unavailable"),
            ):
                report = apply_contextual_cta_overlay(
                    output_dir=root,
                    final_path=final_path,
                    narration_path=narration,
                    script=script,
                )

            self.assertEqual(
                report["render_status"],
                "render_error_fallback_to_uncarded_video",
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

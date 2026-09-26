from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clean_v2.contracts import validate_plan
from clean_v2.cover_lite import (
    build_cover_lite,
    render_cover,
    resolve_cover_text,
    run_cover_lite_fail_soft,
    select_cover_source,
)
from clean_v2.pipeline import CleanV2Pipeline, _planning_prompt


class CoverLiteTests(unittest.TestCase):
    def _short_plan(self) -> dict:
        return {
            "title": "لماذا تفشل خطط إدارة الوقت",
            "promise": "فكرة عملية أوضح",
            "cover_text": "وقتك ليس المشكلة",
            "cta": "",
            "sections": [
                {
                    "id": "s1",
                    "heading": "ضغط القائمة",
                    "purpose": "فتح التوتر",
                    "cover_text": "القائمة تعطلك",
                    "visual_query_en": "overloaded notebook task list",
                    "visual_query_alt_en": "hesitant hand crowded planner",
                },
                {
                    "id": "s2",
                    "heading": "سبب التردد",
                    "purpose": "شرح السبب",
                    "cover_text": "لماذا تتردد؟",
                    "visual_query_en": "hesitant hand at desk",
                    "visual_query_alt_en": "unfinished task note closeup",
                },
                {
                    "id": "s3",
                    "heading": "مهمة واحدة",
                    "purpose": "تقديم الحل",
                    "cover_text": "مهمة واحدة تكفي",
                    "visual_query_en": "one task circled notebook",
                    "visual_query_alt_en": "single checked task planner",
                },
            ],
        }

    def test_plan_preserves_cover_copy_without_making_it_a_gate(self) -> None:
        brief = {"format": "short"}
        plan = validate_plan(self._short_plan(), brief)
        self.assertEqual(plan["cover_text"], "وقتك ليس المشكلة")
        self.assertEqual(plan["sections"][0]["cover_text"], "القائمة تعطلك")

        legacy = self._short_plan()
        legacy.pop("cover_text")
        for section in legacy["sections"]:
            section.pop("cover_text")
        validated_legacy = validate_plan(legacy, brief)
        self.assertNotIn("cover_text", validated_legacy)
        self.assertTrue(all("cover_text" not in row for row in validated_legacy["sections"]))

    def test_derived_short_uses_selected_section_copy(self) -> None:
        plan = self._short_plan()
        self.assertEqual(
            resolve_cover_text(plan, section_id="s2"),
            "لماذا تتردد؟",
        )
        self.assertEqual(resolve_cover_text(plan), "وقتك ليس المشكلة")

    def test_source_selection_prefers_matching_section_then_hook(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            visuals = root / "visuals"
            visuals.mkdir()
            for name in ("hook.mp4", "body.mp4"):
                (visuals / name).write_bytes(b"x" * 2048)
            (root / "rights-manifest.json").write_text(
                json.dumps(
                    {
                        "assets": [
                            {
                                "local_file": "hook.mp4",
                                "section_id": "s1",
                                "role": "hook",
                                "source_actual": "ai_still",
                            },
                            {
                                "local_file": "body.mp4",
                                "section_id": "s2",
                                "role": "body",
                                "source_actual": "stock_motion",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            source, report = select_cover_source(root, section_id="s2")
            self.assertEqual(source.name, "body.mp4")
            self.assertEqual(report["section_id"], "s2")

            source, report = select_cover_source(root)
            self.assertEqual(source.name, "hook.mp4")
            self.assertEqual(report["role"], "hook")

    def test_landscape_source_gets_safe_vertical_reframe_for_short(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            source.write_bytes(b"x" * 2048)
            destination = root / "cover.jpg"

            def fake_run(command, *, timeout=180):
                Path(command[-1]).write_bytes(b"jpg")
                return mock.Mock(stdout="", stderr="")

            with mock.patch("clean_v2.cover_lite._probe_dimensions", return_value=(1920, 1080)), mock.patch(
                "clean_v2.cover_lite._run", side_effect=fake_run
            ) as run:
                report = render_cover(
                    source,
                    destination,
                    text="القائمة تعطلك",
                    fmt="short",
                )

            command = run.call_args.args[0]
            filters = command[command.index("-filter_complex") + 1]
            self.assertIn("boxblur=20:2", filters)
            self.assertIn("overlay=(W-w)/2:(H-h)/2", filters)
            self.assertEqual((report["width"], report["height"]), (1080, 1920))
            self.assertTrue(destination.is_file())

    def test_failure_never_blocks_video_and_adds_no_provider_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = run_cover_lite_fail_soft(
                output_dir=root,
                plan=self._short_plan(),
                fmt="short",
                final_path=root / "missing-final.mp4",
            )
            self.assertEqual(report["status"], "skipped_failed")
            self.assertEqual(report["provider_calls_added"], 0)
            self.assertFalse(report["new_ai_stage"])
            self.assertFalse(report["new_quality_gate"])
            self.assertTrue((root / "cover-lite.json").is_file())

    def test_planning_and_pipeline_wire_cover_lite_without_new_stage(self) -> None:
        planning_source = inspect.getsource(_planning_prompt)
        pipeline_source = inspect.getsource(CleanV2Pipeline.run)
        self.assertIn("COVER_LITE is metadata inside this SAME Planning response", planning_source)
        self.assertIn('"cover_text"', planning_source)
        self.assertIn("run_cover_lite_fail_soft", pipeline_source)
        self.assertIn('output_name="podcast-short-cover.jpg"', pipeline_source)
        self.assertIn('output_name="long-short-cover.jpg"', pipeline_source)


if __name__ == "__main__":
    unittest.main()

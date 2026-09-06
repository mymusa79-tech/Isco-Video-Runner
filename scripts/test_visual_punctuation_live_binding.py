from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from scripts import cta_live_binding
from scripts import visual_punctuation_live_binding as binding


class VisualPunctuationTests(unittest.TestCase):
    def _timeline(self):
        return {
            "final_cut_visuals": [
                {"section_id": f"s{i}", "start_seconds": (i - 1) * 40.0, "end_seconds": i * 40.0}
                for i in range(1, 7)
            ]
        }

    def _plan(self):
        return {
            "format": "film",
            "sections": [
                {
                    "id": "s1",
                    "narration": "هذه مقدمة يجب أن تبقى هادئة بلا تدخل بصري مبكر.",
                    "on_screen_text": "بداية هادئة",
                },
                {
                    "id": "s2",
                    "narration": "لكن المشكلة ليست أنك لا تستطيع، بل أنك تنتظر شعورًا لن يأتي.",
                    "on_screen_text": "لا تنتظر الشعور — ابدأ الآن",
                },
                {
                    "id": "s3",
                    "narration": "ما الذي يتغير لو اتخذت القرار الآن؟ الخطوة الصغيرة تكسر الجمود.",
                    "on_screen_text": "خطوة صغيرة تغيّر الاتجاه",
                },
                {
                    "id": "s4",
                    "narration": "لهذا لا تحتاج قفزة كبيرة. يمكنك العودة بحركة بسيطة ومقصودة.",
                    "key_point": "العودة تبدأ بحركة بسيطة",
                },
                {
                    "id": "s5",
                    "narration": "الهدوء هنا ليس استسلامًا، بل مساحة ترى فيها ما يجب فعله.",
                    "on_screen_text": "الهدوء مساحة للرؤية",
                },
                {
                    "id": "s6",
                    "narration": "هذه خاتمة ويجب أن تبقى لها مساحة تنفس أخيرة.",
                    "on_screen_text": "اترك النهاية تتنفس",
                },
            ],
        }

    def test_contextual_selection_is_bounded_nonrandom_and_uses_accent_focus(self):
        result = binding.plan_visual_punctuation(self._plan(), self._timeline())
        self.assertEqual(result["status"], "applied")
        events = result["events"]
        self.assertLessEqual(len(events), binding.MAX_EVENTS)
        self.assertLessEqual(sum(event["kind"] == "dark_slate" for event in events), binding.MAX_SLATES)
        self.assertTrue(all(event["source_authored_or_verbatim"] for event in events))
        self.assertTrue(all(event["focus_text"] for event in events))
        self.assertFalse(result["rules"]["random_placement"])
        self.assertEqual(result["rules"]["provider_calls"], 0)
        self.assertEqual(result["rules"]["accent_rgb"], "#D7A85B")
        self.assertTrue(all(event["section_id"] not in {"s1", "s6"} for event in events))

    def test_reserved_cta_and_m10_intervals_are_never_covered(self):
        reserved = [(40.0, 80.0, "m10"), (80.0, 120.0, "cta")]
        result = binding.plan_visual_punctuation(self._plan(), self._timeline(), reserved=reserved)
        for event in result["events"]:
            start = float(event["start_seconds"])
            end = float(event["end_seconds"])
            self.assertFalse(binding._overlaps(start, end, reserved))

    def test_explicit_quote_or_stat_is_left_to_m10(self):
        plan = self._plan()
        plan["sections"][1]["narration"] = "قال: «هذه عبارة واضحة موجودة حرفيًا داخل النص ولا يجب تكرار بطاقتها»"
        plan["sections"][1]["on_screen_text"] = "عبارة واضحة"
        result = binding.plan_visual_punctuation(plan, self._timeline())
        self.assertNotIn("s2", [event["section_id"] for event in result["events"]])

    def test_ass_has_warm_accent_and_subtle_motion_not_white_only(self):
        event = {
            "kind": "picture_emphasis",
            "start_seconds": 40.0,
            "end_seconds": 43.2,
            "body_text": "لا تنتظر الشعور",
            "focus_text": "ابدأ الآن",
        }
        ass = binding.build_visual_punctuation_ass([event])
        self.assertIn(binding.ACCENT_ASS, ass)
        self.assertIn(binding.PRIMARY_ASS, ass)
        self.assertIn("VPFocus", ass)
        self.assertIn("\\t(0,200,\\fscx103\\fscy103)", ass)
        self.assertIn("ابدأ الآن", ass)

    def test_live_mux_reads_actual_cta_m10_reports_and_renders_before_core_mux(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "plan.json").write_text(json.dumps(self._plan(), ensure_ascii=False), encoding="utf-8")
            (root / "visual-timeline.json").write_text(json.dumps(self._timeline()), encoding="utf-8")
            (root / "cta-plan.json").write_text(
                json.dumps({"schedule": {"start_seconds": 81.0, "end_seconds": 85.0}}),
                encoding="utf-8",
            )
            (root / "m10-cards.json").write_text(
                json.dumps({"cards": [{"start_seconds": 42.0, "end_seconds": 46.0}]}),
                encoding="utf-8",
            )
            source = root / "picture.mp4"
            source.write_bytes(b"v")
            seen: list[Path] = []

            def fake_render(video, events, dest):
                Path(dest).write_bytes(b"punctuated")
                return Path(dest)

            def core_mux(video, narration, output, music=None, **kwargs):
                seen.append(Path(video))
                Path(output).write_bytes(b"final")
                return Path(output)

            original_mux = binding.orchestrator.mux
            binding.orchestrator.mux = core_mux
            try:
                with patch.object(binding, "render_visual_punctuation", side_effect=fake_render):
                    with binding.visual_punctuation_live_scope():
                        result = binding.orchestrator.mux(source, None, root / "final.mp4")
            finally:
                binding.orchestrator.mux = original_mux

            self.assertEqual(result, root / "final.mp4")
            self.assertEqual(seen[0].name, ".visual-punctuation.mp4")
            report = json.loads((root / "visual-punctuation.json").read_text(encoding="utf-8"))
            self.assertEqual(report["production_stage"], "pre_final_mux_after_m10_cta")
            self.assertEqual({item["owner"] for item in report["reserved_intervals"]}, {"cta", "m10"})
            self.assertFalse((root / ".visual-punctuation.mp4").exists())

    def test_render_failure_keeps_prior_cinematic_picture(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "plan.json").write_text(json.dumps(self._plan(), ensure_ascii=False), encoding="utf-8")
            (root / "visual-timeline.json").write_text(json.dumps(self._timeline()), encoding="utf-8")
            source = root / "already-m10-cta.mp4"
            source.write_bytes(b"v")
            seen: list[Path] = []

            def core_mux(video, narration, output, music=None, **kwargs):
                seen.append(Path(video))
                return Path(output)

            original_mux = binding.orchestrator.mux
            binding.orchestrator.mux = core_mux
            try:
                with patch.object(binding, "render_visual_punctuation", side_effect=RuntimeError("synthetic")):
                    with binding.visual_punctuation_live_scope():
                        binding.orchestrator.mux(source, None, root / "final.mp4")
            finally:
                binding.orchestrator.mux = original_mux

            self.assertEqual(seen, [source])
            report = json.loads((root / "visual-punctuation.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "render_error_fallback_to_prior_cinematic_picture")
            self.assertFalse(report["production_blocking"])

    def test_cta_installer_composes_visual_outer_scope_then_cta_scope(self):
        trace: list[str] = []
        original_produce = cta_live_binding.orchestrator.produce

        def core(*args, **kwargs):
            trace.append("core")
            return "ok"

        @contextmanager
        def visual_scope():
            trace.append("visual_enter")
            try:
                yield
            finally:
                trace.append("visual_exit")

        @contextmanager
        def cta_scope():
            trace.append("cta_enter")
            try:
                yield
            finally:
                trace.append("cta_exit")

        cta_live_binding.orchestrator.produce = core
        try:
            with patch.object(cta_live_binding, "visual_punctuation_live_scope", visual_scope), patch.object(
                cta_live_binding, "cta_live_scope", cta_scope
            ):
                cta_live_binding.install_cta_live_binding()
                result = cta_live_binding.orchestrator.produce()
            self.assertEqual(result, "ok")
            self.assertEqual(trace, ["visual_enter", "cta_enter", "core", "cta_exit", "visual_exit"])
            self.assertTrue(getattr(cta_live_binding.orchestrator.produce, "_isco_cta_live_binding", False))
        finally:
            cta_live_binding.orchestrator.produce = original_produce

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg unavailable")
    def test_ffmpeg_renderer_smoke(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source.mp4"
            output = root / "punctuated.mp4"
            subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=c=black:s=1920x1080:r=24:d=4",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(source),
                ],
                check=True,
            )
            event = {
                "kind": "dark_slate",
                "start_seconds": 0.5,
                "end_seconds": 3.2,
                "body_text": "لا تنتظر الشعور",
                "focus_text": "ابدأ الآن",
            }
            rendered = binding.render_visual_punctuation(source, [event], output)
            self.assertEqual(rendered, output)
            self.assertTrue(output.exists())
            self.assertGreater(output.stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()

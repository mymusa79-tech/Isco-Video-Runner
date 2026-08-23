from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import isco_video_agent.orchestrator as orchestrator
from scripts import cta_live_binding as binding


def _plan(cta: str, *, fmt: str = "film"):
    return SimpleNamespace(
        format=fmt,
        cta=cta,
        sections=[
            SimpleNamespace(id=f"s{i}", narration=f"نص القسم {i} يقدم قيمة حقيقية للمشاهد.", key_point=f"فكرة {i}")
            for i in range(1, 7)
        ],
    )


def _timeline():
    return {
        "final_cut_visuals": [
            {"section_id": f"s{i}", "start_seconds": (i - 1) * 40.0, "end_seconds": i * 40.0}
            for i in range(1, 7)
        ]
    }


class CtaLiveBindingTests(unittest.TestCase):
    def test_spoken_cta_is_bound_before_core_consumes_plan(self) -> None:
        plan = _plan("اشترك لتكمل الرحلة معنا")
        original_narration = " ".join(section.narration for section in plan.sections)
        original_build = orchestrator.build_plan
        orchestrator.build_plan = lambda *a, **k: plan
        try:
            with binding.cta_live_scope():
                produced = orchestrator.build_plan("key", "topic", "film", "model")
                joined = " ".join(section.narration for section in produced.sections)
                self.assertNotEqual(joined, original_narration)
                self.assertEqual(joined.count("اشترك لتكمل الرحلة معنا"), 1)
        finally:
            orchestrator.build_plan = original_build

    def test_like_is_visual_only_and_moment_has_no_cta(self) -> None:
        like = _plan("إذا أضافت لك الفكرة شيئًا، يكفيني إعجابك.")
        before = [section.narration for section in like.sections]
        like_binding = binding.bind_contextual_cta(like)
        self.assertTrue(like_binding.visual_only)
        self.assertEqual(before, [section.narration for section in like.sections])

        moment = _plan("اشترك لتكمل الرحلة", fmt="moment")
        moment_binding = binding.bind_contextual_cta(moment)
        self.assertEqual(moment_binding.mode.value, "none")
        self.assertEqual(moment_binding.reason, "moment_no_cta")

    def test_section_timing_uses_current_m7_final_cut(self) -> None:
        ids, durations = binding._section_timing(_plan("اشترك لتكمل الرحلة معنا"), _timeline())
        self.assertEqual(ids, [f"s{i}" for i in range(1, 7)])
        self.assertEqual(durations, [40.0] * 6)

    def test_final_mux_renders_scheduled_overlay_and_writes_nonblocking_report(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "visual-timeline.json").write_text(json.dumps(_timeline()), encoding="utf-8")
            plan = _plan("اشترك لتكمل الرحلة معنا")
            video = root / "picture.mp4"
            narration = root / "narration.wav"
            video.write_bytes(b"video")
            narration.write_bytes(b"audio")
            output = root / "final.mp4"

            original_build = orchestrator.build_plan
            original_mux = orchestrator.mux
            orchestrator.build_plan = lambda *a, **k: plan
            orchestrator.mux = lambda video, narration, output, music=None, **kwargs: Path(output)
            try:
                with patch.object(binding, "render_cta_overlay", side_effect=lambda src, b, s, dest: Path(dest)) as render:
                    with binding.cta_live_scope():
                        orchestrator.build_plan("key", "topic", "film", "model")
                        result = orchestrator.mux(video, narration, output)
                self.assertEqual(result, output)
                render.assert_called_once()
                report = json.loads((root / "cta-plan.json").read_text(encoding="utf-8"))
                self.assertEqual(report["status"], "scheduled")
                self.assertEqual(report["production_stage"], "pre_final_mux")
                self.assertEqual(report["spoken_binding_stage"], "pre_tts_plan_binding")
                self.assertFalse(report["production_blocking"])
                self.assertEqual(report["rules"]["provider_calls"], 0)
            finally:
                orchestrator.build_plan = original_build
                orchestrator.mux = original_mux

    def test_render_error_falls_back_to_unmodified_video(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "visual-timeline.json").write_text(json.dumps(_timeline()), encoding="utf-8")
            plan = _plan("اشترك لتكمل الرحلة معنا")
            video = root / "picture.mp4"
            narration = root / "narration.wav"
            output = root / "final.mp4"
            video.write_bytes(b"video")
            narration.write_bytes(b"audio")
            seen: list[Path] = []

            original_build = orchestrator.build_plan
            original_mux = orchestrator.mux
            orchestrator.build_plan = lambda *a, **k: plan
            orchestrator.mux = lambda video, narration, output, music=None, **kwargs: seen.append(Path(video)) or Path(output)
            try:
                with patch.object(binding, "render_cta_overlay", side_effect=RuntimeError("synthetic")):
                    with binding.cta_live_scope():
                        orchestrator.build_plan("key", "topic", "film", "model")
                        orchestrator.mux(video, narration, output)
                self.assertEqual(seen, [video])
                report = json.loads((root / "cta-plan.json").read_text(encoding="utf-8"))
                self.assertEqual(report["status"], "render_error_fallback_to_unmodified_video")
            finally:
                orchestrator.build_plan = original_build
                orchestrator.mux = original_mux


if __name__ == "__main__":
    unittest.main()

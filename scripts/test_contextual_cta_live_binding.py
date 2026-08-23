from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import isco_video_agent.orchestrator as orchestrator
import scripts.contextual_cta_live_binding as live


def _plan(*, fmt: str = "film", cta: str = "ما رأيك؟ اكتب تعليقك."):
    return SimpleNamespace(
        format=fmt,
        cta=cta,
        sections=[
            SimpleNamespace(id=f"s{i}", narration=f"نص القسم {i}.", emotion="calm")
            for i in range(1, 6)
        ],
    )


class ContextualCtaLiveBindingTests(unittest.TestCase):
    def _fake_tts(self, *args, **kwargs):
        output = Path(kwargs["output"])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"wav")
        return output

    def _fake_mux(self, video, narration, output, **kwargs):
        del video, narration, kwargs
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"original-final" * 200)
        return output

    def test_spoken_cta_is_bound_before_tts_and_visual_is_pre_final_qa(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = _plan()

            def fake_render(video, binding, schedule, dest):
                self.assertTrue(Path(video).is_file())
                self.assertEqual(binding.anchor_section_id, "s3")
                self.assertGreaterEqual(schedule.start_seconds, 30.5)
                self.assertLessEqual(schedule.end_seconds, 88.0)
                dest = Path(dest)
                dest.write_bytes(b"cta-rendered" * 200)
                return dest

            with (
                patch.object(orchestrator, "build_plan", return_value=plan),
                patch.object(orchestrator, "_synthesize_tts_section", side_effect=self._fake_tts),
                patch.object(orchestrator, "mux", side_effect=self._fake_mux),
                patch.object(orchestrator, "duration", return_value=20.0),
                patch.object(live, "render_cta_overlay", side_effect=fake_render),
            ):
                with live.contextual_cta_live_scope():
                    bound_plan = orchestrator.build_plan()
                    self.assertIn("ما رأيك؟", bound_plan.sections[2].narration)
                    for index in range(5):
                        orchestrator._synthesize_tts_section(
                            None,
                            None,
                            None,
                            task_id=f"TTS_SECTION_{index+1:02d}",
                            api_key="x",
                            transcript=bound_plan.sections[index].narration,
                            output=root / "audio" / f"{index+1:02d}.wav",
                            model="m",
                            voice="v",
                            style="s",
                        )
                    final = orchestrator.mux(root / "picture.mp4", root / "narration.wav", root / "final.mp4")

            self.assertEqual(final.read_bytes(), b"cta-rendered" * 200)
            report = json.loads((root / "contextual-cta.json").read_text(encoding="utf-8"))
            self.assertEqual(report["mode"], "comment")
            self.assertEqual(report["render_status"], "applied")
            self.assertEqual(report["production_stage"], "spoken_pre_tts_visual_pre_final_qa")
            self.assertFalse(report["creates_cut"])
            self.assertFalse(report["changes_pacing"])
            self.assertEqual(report["provider_calls"], 0)

    def test_visual_render_failure_falls_back_to_already_muxed_final(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = _plan(cta="اشترك لتكمل الرحلة")
            original_bytes = b"original-final" * 200

            def fake_mux(video, narration, output, **kwargs):
                del video, narration, kwargs
                output = Path(output)
                output.write_bytes(original_bytes)
                return output

            with (
                patch.object(orchestrator, "build_plan", return_value=plan),
                patch.object(orchestrator, "_synthesize_tts_section", side_effect=self._fake_tts),
                patch.object(orchestrator, "mux", side_effect=fake_mux),
                patch.object(orchestrator, "duration", return_value=20.0),
                patch.object(live, "render_cta_overlay", side_effect=RuntimeError("local render failed")),
            ):
                with live.contextual_cta_live_scope():
                    orchestrator.build_plan()
                    for index in range(5):
                        orchestrator._synthesize_tts_section(
                            None,
                            None,
                            None,
                            task_id=f"TTS_SECTION_{index+1:02d}",
                            api_key="x",
                            transcript="x",
                            output=root / "audio" / f"{index+1:02d}.wav",
                            model="m",
                            voice="v",
                            style="s",
                        )
                    final = orchestrator.mux(root / "picture.mp4", root / "narration.wav", root / "final.mp4")

            self.assertEqual(final.read_bytes(), original_bytes)
            report = json.loads((root / "contextual-cta.json").read_text(encoding="utf-8"))
            self.assertEqual(report["render_status"], "fallback_original_final")
            self.assertEqual(report["render_error_class"], "RuntimeError")

    def test_moment_remains_cta_free(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = _plan(fmt="moment")
            with (
                patch.object(orchestrator, "build_plan", return_value=plan),
                patch.object(orchestrator, "_synthesize_tts_section", side_effect=self._fake_tts),
                patch.object(orchestrator, "mux", side_effect=self._fake_mux),
                patch.object(orchestrator, "duration", return_value=20.0),
                patch.object(live, "render_cta_overlay") as renderer,
            ):
                with live.contextual_cta_live_scope():
                    orchestrator.build_plan()
                    orchestrator.mux(root / "picture.mp4", None, root / "final.mp4")
            renderer.assert_not_called()
            report = json.loads((root / "contextual-cta.json").read_text(encoding="utf-8"))
            self.assertEqual(report["mode"], "none")
            self.assertIsNone(report["schedule"])
            self.assertEqual(report["render_status"], "not_scheduled")


if __name__ == "__main__":
    unittest.main()

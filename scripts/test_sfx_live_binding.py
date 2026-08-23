from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import sfx_live_binding as binding


class SfxLiveBindingTests(unittest.TestCase):
    def _timeline(self) -> dict:
        return {
            "schema_version": "cinematic.m7.visual_timeline.v2",
            "sections": [
                {"section_id": "s1", "start_seconds": 0.0, "end_seconds": 40.0},
                {"section_id": "s2", "start_seconds": 40.0, "end_seconds": 85.0},
                {"section_id": "s3", "start_seconds": 85.0, "end_seconds": 130.0},
            ],
        }

    def test_final_longform_mux_uses_mixed_narration_from_m7_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "visual-timeline.json").write_text(json.dumps(self._timeline()), encoding="utf-8")
            narration = root / "narration-mastered.wav"
            narration.write_bytes(b"narr")
            final = root / "final.mp4"
            event = SimpleNamespace(sfx_id="soft_hit_01")
            mux_calls = []

            def original_mux(video, narration_arg, output, music=None, **kwargs):
                mux_calls.append((Path(narration_arg), Path(output), music, kwargs))
                return Path(output)

            def fake_mix(_narration, _events, _library, dest):
                Path(dest).write_bytes(b"mixed")
                return Path(dest)

            with patch.object(binding.orchestrator, "mux", original_mux), patch.object(
                binding, "plan_sfx_accents", return_value=[event]
            ), patch.object(
                binding,
                "sfx_plan_document",
                return_value={"events": [{"event_id": "sfx-01"}], "zero_additional_ai_calls": True},
            ), patch.object(binding, "materialize_sfx_library") as materialize, patch.object(
                binding, "mix_sfx_into_narration", side_effect=fake_mix
            ) as mix_sfx:
                with binding.sfx_live_scope():
                    result = binding.orchestrator.mux(Path("picture.mp4"), narration, final, Path("music.wav"), target_lufs=-16)

            self.assertEqual(result, final)
            materialize.assert_called_once_with(root / "sfx" / "library")
            mix_sfx.assert_called_once()
            self.assertEqual(mux_calls[0][0], root / "narration-sfx.wav")
            self.assertEqual(mux_calls[0][1], final)
            self.assertEqual(mux_calls[0][3]["target_lufs"], -16)
            plan = json.loads((root / "sfx-plan.json").read_text(encoding="utf-8"))
            self.assertEqual(plan["status"], "mixed")
            self.assertEqual(plan["production_stage"], "post_mastering_pre_final_mux")
            self.assertEqual(plan["source_narration"], "narration-mastered.wav")

    def test_no_events_preserves_mastered_narration_and_records_plan(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "visual-timeline.json").write_text(json.dumps(self._timeline()), encoding="utf-8")
            narration = root / "narration-mastered.wav"
            narration.write_bytes(b"narr")
            seen = []

            def original_mux(_video, narration_arg, output, music=None, **kwargs):
                seen.append(Path(narration_arg))
                return Path(output)

            with patch.object(binding.orchestrator, "mux", original_mux), patch.object(
                binding, "plan_sfx_accents", return_value=[]
            ), patch.object(
                binding, "sfx_plan_document", return_value={"events": [], "zero_additional_ai_calls": True}
            ), patch.object(binding, "materialize_sfx_library") as materialize:
                with binding.sfx_live_scope():
                    binding.orchestrator.mux(Path("picture.mp4"), narration, root / "final.mp4")

            self.assertEqual(seen, [narration])
            materialize.assert_not_called()
            plan = json.loads((root / "sfx-plan.json").read_text(encoding="utf-8"))
            self.assertEqual(plan["status"], "no_events")

    def test_missing_or_invalid_m7_timeline_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            narration = root / "narration-mastered.wav"
            narration.write_bytes(b"narr")
            with patch.object(binding.orchestrator, "mux", lambda *a, **k: Path(a[2])):
                with self.assertRaisesRegex(RuntimeError, "requires_live_m7_visual_timeline"):
                    with binding.sfx_live_scope():
                        binding.orchestrator.mux(Path("picture.mp4"), narration, root / "final.mp4")

            (root / "visual-timeline.json").write_text("{}", encoding="utf-8")
            with patch.object(binding.orchestrator, "mux", lambda *a, **k: Path(a[2])):
                with self.assertRaisesRegex(RuntimeError, "invalid_m7_sections_contract"):
                    with binding.sfx_live_scope():
                        binding.orchestrator.mux(Path("picture.mp4"), narration, root / "final.mp4")

    def test_nonfinal_or_non_narrated_mux_passes_through(self) -> None:
        seen = []
        def original_mux(video, narration, output, music=None, **kwargs):
            seen.append((narration, Path(output)))
            return Path(output)
        with patch.object(binding.orchestrator, "mux", original_mux), patch.object(binding, "plan_sfx_accents") as planner:
            with binding.sfx_live_scope():
                binding.orchestrator.mux(Path("p.mp4"), Path("n.wav"), Path("preview.mp4"))
                binding.orchestrator.mux(Path("p.mp4"), None, Path("final.mp4"), Path("music.wav"))
        self.assertEqual(len(seen), 2)
        planner.assert_not_called()

    def test_mux_hook_is_restored_on_failure(self) -> None:
        def original_mux(*args, **kwargs):
            return Path(args[2])
        with patch.object(binding.orchestrator, "mux", original_mux):
            with self.assertRaises(RuntimeError):
                with binding.sfx_live_scope():
                    binding.orchestrator.mux(Path("p.mp4"), Path("n.wav"), Path("final.mp4"))
            self.assertIs(binding.orchestrator.mux, original_mux)


if __name__ == "__main__":
    unittest.main()

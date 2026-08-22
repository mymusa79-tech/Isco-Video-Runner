from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import isco_video_agent.orchestrator as orchestrator
import scripts.cinematic_v2_runtime_installer as installer


class CinematicV2RuntimeInstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.duration = orchestrator.duration
        self.prepare_clip = orchestrator.prepare_clip
        self.concat_video = orchestrator.concat_video
        self.marker = getattr(orchestrator, installer._MARKER, False)
        installer._reset_run_state()

    def tearDown(self) -> None:
        orchestrator.duration = self.duration
        orchestrator.prepare_clip = self.prepare_clip
        orchestrator.concat_video = self.concat_video
        if self.marker:
            setattr(orchestrator, installer._MARKER, True)
        else:
            try:
                delattr(orchestrator, installer._MARKER)
            except AttributeError:
                pass
        installer._reset_run_state()

    def test_installer_patches_only_three_runtime_seams_and_is_idempotent(self) -> None:
        installer.install_cinematic_v2_runtime()
        first = (orchestrator.duration, orchestrator.prepare_clip, orchestrator.concat_video)
        self.assertTrue(getattr(orchestrator, installer._MARKER))
        installer.install_cinematic_v2_runtime()
        self.assertEqual(first, (orchestrator.duration, orchestrator.prepare_clip, orchestrator.concat_video))

    def test_duration_capture_records_numbered_tts_wavs_only(self) -> None:
        with patch.object(installer, "_original_duration", side_effect=[12.25, 4.0, 9.75]):
            self.assertEqual(installer._duration_with_timeline_capture(Path("output/x/audio/01.wav")), 12.25)
            self.assertEqual(installer._duration_with_timeline_capture(Path("output/x/narration.wav")), 4.0)
            self.assertEqual(installer._duration_with_timeline_capture(Path("output/x/audio/02.wav")), 9.75)
        self.assertEqual(installer._section_durations, [12.25, 9.75])

    def test_prepare_runs_m8_before_legacy_and_keeps_report(self) -> None:
        report = {"classification": "BT709_SDR_EXPLICIT", "creative_grade_applied": False}
        with patch.object(installer, "prepare_clip_with_m8", return_value=(Path("dest.mp4"), report)) as call:
            result = installer._prepare_clip_with_cinematic_v2(
                Path("raw.mp4"), Path("dest.mp4"), 8.0, False, 30
            )
        self.assertEqual(result, Path("dest.mp4"))
        self.assertEqual(installer._color_reports[0]["classification"], "BT709_SDR_EXPLICIT")
        self.assertFalse(installer._color_reports[0]["creative_grade_applied"])
        self.assertIs(call.call_args.kwargs["legacy_prepare_clip"], installer._original_prepare_clip)

    def test_concat_writes_timeline_and_cards_after_hard_cut_concat(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output = root / "picture.mp4"
            (root / "plan.json").write_text(
                json.dumps(
                    {
                        "format": "film",
                        "sections": [
                            {"id": "sec_1", "narration": "نص", "key_point": "أ", "emotion": "calm"},
                            {"id": "sec_2", "narration": "نص", "key_point": "ب", "emotion": "calm"},
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            installer._section_durations[:] = [20.0, 20.0]
            with patch.object(installer, "_original_concat_video", return_value=output) as concat, patch.object(
                installer, "apply_planned_cards", return_value=root / "carded.mp4"
            ) as cards:
                result = installer._concat_video_with_cinematic_v2([Path("a.mp4"), Path("b.mp4")], output)
            self.assertEqual(result, root / "carded.mp4")
            concat.assert_called_once()
            cards.assert_called_once()
            timeline = json.loads((root / "cinematic-timeline.json").read_text(encoding="utf-8"))
            self.assertTrue(all(x["kind"] == "HARD_CUT" for x in timeline["transition_anchors"]))
            self.assertEqual(timeline["audio_mastering"]["status"], "WAIT_TTS_RESULT")
            self.assertEqual(timeline["sfx"]["status"], "WAIT_HUMAN_CURATION")

    def test_concat_fails_closed_on_timeline_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output = root / "picture.mp4"
            (root / "plan.json").write_text(
                json.dumps({"format": "film", "sections": [{"id": "sec_1"}, {"id": "sec_2"}]}),
                encoding="utf-8",
            )
            installer._section_durations[:] = [10.0]
            with self.assertRaisesRegex(RuntimeError, "does not match plan"):
                installer._concat_video_with_cinematic_v2([], output)

    def test_non_hard_cut_is_fail_closed_until_renderer_binding_exists(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output = root / "picture.mp4"
            (root / "plan.json").write_text(json.dumps({"format": "film", "sections": [{"id": "sec_1"}]}), encoding="utf-8")
            installer._section_durations[:] = [10.0]
            fake = {
                "transition_anchors": [{"kind": "DISSOLVE"}],
                "archive_opportunities": [],
                "placements": [],
            }
            with patch.object(installer, "build_cinematic_timeline", return_value=fake):
                with self.assertRaisesRegex(RuntimeError, "transition renderer blocked"):
                    installer._concat_video_with_cinematic_v2([], output)

    def test_archive_opportunity_is_fail_closed_until_acquisition_binding_exists(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output = root / "picture.mp4"
            (root / "plan.json").write_text(json.dumps({"format": "film", "sections": [{"id": "sec_1"}]}), encoding="utf-8")
            installer._section_durations[:] = [10.0]
            fake = {
                "transition_anchors": [],
                "archive_opportunities": [{"query": "artifact"}],
                "placements": [],
            }
            with patch.object(installer, "build_cinematic_timeline", return_value=fake):
                with self.assertRaisesRegex(RuntimeError, "archive placement blocked"):
                    installer._concat_video_with_cinematic_v2([], output)


if __name__ == "__main__":
    unittest.main()

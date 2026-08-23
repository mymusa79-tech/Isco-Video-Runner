from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import narrative_music_dynamics as dynamics


def _shot(start: float, end: float, role: str | None, *, hei_intent: str = "payoff") -> dict:
    shot = {
        "shot_id": f"shot-{start:g}",
        "start_seconds": start,
        "end_seconds": end,
        "human_editorial_intent": {
            "status": "bound",
            "intent": hei_intent,
            "hero": True,
        },
    }
    if role is not None:
        shot["adaptive_pacing"] = {
            "version": "m7-adaptive-semantic-pacing-v1",
            "role": role,
            "reason": "test",
            "target_shots": 1,
        }
    return shot


def _timeline(*shots: dict, duration_seconds: float) -> dict:
    return {
        "duration_seconds": duration_seconds,
        "human_editorial_intent": {"status": "bound"},
        "final_cut_visuals": list(shots),
    }


class NarrativeMusicDynamicsTests(unittest.TestCase):
    def test_target_adjustments_follow_m7_pacing_and_are_subtle(self) -> None:
        self.assertEqual(dynamics._target_db("linger"), -0.9)
        self.assertEqual(dynamics._target_db("steady"), 0.0)
        self.assertEqual(dynamics._target_db("build"), 0.6)
        self.assertEqual(dynamics._target_db("accelerate"), 1.1)
        self.assertEqual(dynamics._target_db("release"), -0.8)
        self.assertEqual(dynamics._target_db("unknown"), 0.0)
        for role in dynamics._PACING_ROLE_DB:
            self.assertLessEqual(abs(dynamics._target_db(role)), dynamics.MAX_ABS_ADJUSTMENT_DB)

    def test_payoff_human_intent_cannot_override_release_pacing(self) -> None:
        timeline = _timeline(
            _shot(0.0, 20.0, "linger", hei_intent="establishing_context"),
            _shot(20.0, 40.0, "build", hei_intent="emotional_reinforcement"),
            _shot(40.0, 60.0, "release", hei_intent="payoff"),
            duration_seconds=60.0,
        )
        schedule = dynamics._coverage_schedule(timeline, narrative_seconds=60.0, total_seconds=64.0)
        self.assertEqual(schedule[0]["pacing_role"], "linger")
        self.assertEqual(schedule[0]["adjustment_db"], -0.9)
        self.assertEqual(schedule[1]["pacing_role"], "build")
        self.assertEqual(schedule[1]["adjustment_db"], 0.6)
        self.assertEqual(schedule[2]["pacing_role"], "release")
        self.assertEqual(schedule[2]["adjustment_db"], -0.8)
        self.assertEqual(schedule[-1]["pacing_role"], "outro_quiet_tail")
        self.assertEqual(schedule[-1]["adjustment_db"], dynamics.OUTRO_ADJUSTMENT_DB)

    def test_same_scene_shot_splits_do_not_pump_music(self) -> None:
        timeline = _timeline(
            _shot(0.0, 8.0, "accelerate"),
            _shot(8.0, 15.0, "accelerate"),
            _shot(15.0, 22.0, "accelerate"),
            duration_seconds=22.0,
        )
        schedule = dynamics._coverage_schedule(timeline, narrative_seconds=22.0, total_seconds=22.0)
        self.assertEqual(len(schedule), 1)
        self.assertEqual(schedule[0]["pacing_role"], "accelerate")
        self.assertEqual(schedule[0]["start_seconds"], 0.0)
        self.assertEqual(schedule[0]["end_seconds"], 22.0)

    def test_missing_pacing_role_is_neutral_not_hei_driven(self) -> None:
        timeline = _timeline(
            _shot(0.0, 15.0, None, hei_intent="payoff"),
            _shot(15.0, 30.0, "steady", hei_intent="payoff"),
            duration_seconds=30.0,
        )
        raw = dynamics._raw_segments(timeline)
        self.assertEqual(raw[0]["adjustment_db"], 0.0)
        self.assertEqual(raw[0]["derivation"], "neutral_fallback_no_m7_pacing_role")
        self.assertEqual(raw[1]["adjustment_db"], 0.0)

    def test_duration_mismatch_is_detected_in_internal_schedule(self) -> None:
        timeline = _timeline(_shot(0.0, 30.0, "build"), duration_seconds=30.0)
        with self.assertRaisesRegex(dynamics.NarrativeMusicDynamicsError, "timeline_narration_duration_mismatch"):
            dynamics._coverage_schedule(timeline, narrative_seconds=34.0, total_seconds=34.0)

    def test_ramps_are_continuous_and_expression_is_finite(self) -> None:
        schedule = [
            {"start_seconds": 0.0, "end_seconds": 10.0, "adjustment_db": -0.9},
            {"start_seconds": 10.0, "end_seconds": 20.0, "adjustment_db": 1.1},
            {"start_seconds": 20.0, "end_seconds": 30.0, "adjustment_db": -0.8},
        ]
        pieces = dynamics._ramp_pieces(schedule)
        self.assertEqual(pieces[0]["start"], 0.0)
        self.assertEqual(pieces[-1]["end"], 30.0)
        for left, right in zip(pieces, pieces[1:]):
            self.assertAlmostEqual(left["end"], right["start"], places=6)
            self.assertAlmostEqual(left["gain_end"], right["gain_start"], places=6)
        expression = dynamics._ffmpeg_volume_expression(pieces)
        self.assertIn("if(lt(t,", expression)
        self.assertNotIn("nan", expression.lower())
        self.assertNotIn("inf", expression.lower())
        for piece in pieces:
            self.assertTrue(math.isfinite(piece["gain_start"]))
            self.assertTrue(math.isfinite(piece["gain_end"]))

    def test_timeline_without_adaptive_pacing_is_exact_passthrough(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "visual-timeline.json").write_text(
                json.dumps(_timeline(_shot(0.0, 20.0, None), duration_seconds=20.0)),
                encoding="utf-8",
            )
            narration = root / "narration.wav"
            music = root / "music.wav"
            video = root / "picture.mp4"
            for path in (narration, music, video):
                path.write_bytes(b"x")
            calls = []

            def original(video_arg, narration_arg, output_arg, music=None, **kwargs):
                calls.append((Path(video_arg), Path(narration_arg), Path(output_arg), Path(music), dict(kwargs)))
                return Path(output_arg)

            with patch.object(dynamics.orchestrator, "mux", original):
                dynamics.install_narrative_music_dynamics()
                result = dynamics.orchestrator.mux(video, narration, root / "final.mp4", music=music, target_lufs=-16.0)

            self.assertEqual(result, root / "final.mp4")
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][3], music)
            report = json.loads((root / "narrative-music-dynamics.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "not_applicable")
            self.assertFalse(report["production_blocked"])

    def test_nonadaptive_baseline_mux_failure_propagates_once_without_polish_retry(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "visual-timeline.json").write_text(
                json.dumps(_timeline(_shot(0.0, 20.0, None), duration_seconds=20.0)),
                encoding="utf-8",
            )
            narration = root / "narration.wav"
            music = root / "music.wav"
            video = root / "picture.mp4"
            for path in (narration, music, video):
                path.write_bytes(b"x")
            calls = []

            def original(video_arg, narration_arg, output_arg, music=None, **kwargs):
                calls.append(Path(music))
                raise RuntimeError("baseline-mux-failure")

            with patch.object(dynamics.orchestrator, "mux", original):
                dynamics.install_narrative_music_dynamics()
                with self.assertRaisesRegex(RuntimeError, "baseline-mux-failure"):
                    dynamics.orchestrator.mux(video, narration, root / "final.mp4", music=music)

            self.assertEqual(calls, [music])
            report = json.loads((root / "narrative-music-dynamics.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "not_applicable")
            self.assertNotEqual(report.get("status"), "fallback_original_music")

    def test_polish_pre_render_failure_falls_back_to_original_music(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "visual-timeline.json").write_text(
                json.dumps(_timeline(_shot(0.0, 20.0, "build"), duration_seconds=20.0)),
                encoding="utf-8",
            )
            narration = root / "narration.wav"
            music = root / "music.wav"
            video = root / "picture.mp4"
            for path in (narration, music, video):
                path.write_bytes(b"x")
            seen = []

            def original(video_arg, narration_arg, output_arg, music=None, **kwargs):
                seen.append(Path(music))
                return Path(output_arg)

            with patch.object(dynamics.orchestrator, "mux", original), \
                 patch.object(dynamics, "duration", return_value=20.0), \
                 patch.object(dynamics, "_render_dynamic_music", side_effect=RuntimeError("synthetic-polish-failure")):
                dynamics.install_narrative_music_dynamics()
                result = dynamics.orchestrator.mux(video, narration, root / "final.mp4", music=music)

            self.assertEqual(result, root / "final.mp4")
            self.assertEqual(seen, [music])
            report = json.loads((root / "narrative-music-dynamics.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "fallback_original_music")
            self.assertFalse(report["production_blocked"])
            self.assertIn("synthetic-polish-failure", report["reason"])

    def test_adaptive_wrapper_supplies_transient_music_then_deletes_it(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            timeline = _timeline(
                _shot(0.0, 10.0, "linger"),
                _shot(10.0, 20.0, "release"),
                duration_seconds=20.0,
            )
            (root / "visual-timeline.json").write_text(json.dumps(timeline), encoding="utf-8")
            narration = root / "narration.wav"
            music = root / "music.wav"
            video = root / "picture.mp4"
            for path in (narration, music, video):
                path.write_bytes(b"x")
            seen = {}

            def original(video_arg, narration_arg, output_arg, music=None, **kwargs):
                seen["music"] = Path(music)
                seen["kwargs"] = dict(kwargs)
                self.assertTrue(Path(music).is_file())
                return Path(output_arg)

            def fake_render(source, dest, *, total_seconds, expression):
                self.assertEqual(Path(source), music)
                self.assertGreater(total_seconds, 20.0)
                self.assertIn("if(lt(t,", expression)
                Path(dest).write_bytes(b"dynamic")

            with patch.object(dynamics.orchestrator, "mux", original), \
                 patch.object(dynamics, "duration", return_value=20.0), \
                 patch.object(dynamics, "_render_dynamic_music", side_effect=fake_render):
                dynamics.install_narrative_music_dynamics()
                result = dynamics.orchestrator.mux(video, narration, root / "final.mp4", music=music, target_lufs=-16.0)

            self.assertEqual(result, root / "final.mp4")
            self.assertEqual(seen["kwargs"]["target_lufs"], -16.0)
            self.assertEqual(seen["music"].name, "narrative-music-dynamics.wav")
            self.assertFalse((root / "narrative-music-dynamics.wav").exists())
            report = json.loads((root / "narrative-music-dynamics.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "applied")
            self.assertEqual(report["mode"], "m7_adaptive_pacing_music_dynamics")
            self.assertEqual(report["role_adjustments_db"]["release"], -0.8)
            self.assertFalse(report["production_blocked"])
            self.assertTrue(report["music_bed_only"])
            self.assertEqual(report["ai_calls_added"], 0)
            self.assertFalse(report["narration_authority_changed"])
            self.assertFalse(report["rights_provenance_changed"])

    def test_dynamic_mux_failure_retries_once_with_original_music(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "visual-timeline.json").write_text(
                json.dumps(_timeline(_shot(0.0, 20.0, "build"), duration_seconds=20.0)),
                encoding="utf-8",
            )
            narration = root / "narration.wav"
            music = root / "music.wav"
            video = root / "picture.mp4"
            for path in (narration, music, video):
                path.write_bytes(b"x")
            seen = []

            def original(video_arg, narration_arg, output_arg, music=None, **kwargs):
                seen.append(Path(music))
                if Path(music).name == "narrative-music-dynamics.wav":
                    raise RuntimeError("dynamic-only-failure")
                return Path(output_arg)

            def fake_render(source, dest, *, total_seconds, expression):
                Path(dest).write_bytes(b"dynamic")

            with patch.object(dynamics.orchestrator, "mux", original), \
                 patch.object(dynamics, "duration", return_value=20.0), \
                 patch.object(dynamics, "_render_dynamic_music", side_effect=fake_render):
                dynamics.install_narrative_music_dynamics()
                result = dynamics.orchestrator.mux(video, narration, root / "final.mp4", music=music)

            self.assertEqual(result, root / "final.mp4")
            self.assertEqual([path.name for path in seen], ["narrative-music-dynamics.wav", "music.wav"])
            report = json.loads((root / "narrative-music-dynamics.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "fallback_original_music")
            self.assertIn("dynamic_mux_retry_original", report["reason"])


if __name__ == "__main__":
    unittest.main()

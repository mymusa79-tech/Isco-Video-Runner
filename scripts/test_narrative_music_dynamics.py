from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import narrative_music_dynamics as dynamics


def _shot(start: float, end: float, intent: str, *, hero: bool = False) -> dict:
    return {
        "shot_id": f"shot-{start:g}",
        "start_seconds": start,
        "end_seconds": end,
        "human_editorial_intent": {
            "status": "bound",
            "intent": intent,
            "hero": hero,
        },
    }


def _timeline(*shots: dict, duration_seconds: float) -> dict:
    return {
        "duration_seconds": duration_seconds,
        "human_editorial_intent": {"status": "bound"},
        "final_cut_visuals": list(shots),
    }


class NarrativeMusicDynamicsTests(unittest.TestCase):
    def test_target_adjustments_are_subtle_and_bounded(self) -> None:
        self.assertEqual(dynamics._target_db("establishing_context"), -0.8)
        self.assertEqual(dynamics._target_db("payoff"), 1.8)
        self.assertEqual(dynamics._target_db("payoff", hero=True), 2.0)
        self.assertEqual(dynamics._target_db("unknown"), 0.0)
        for intent in dynamics._INTENT_DB:
            self.assertLessEqual(abs(dynamics._target_db(intent, hero=True)), dynamics.MAX_ABS_ADJUSTMENT_DB)

    def test_schedule_uses_m7_intent_and_keeps_quiet_outro(self) -> None:
        timeline = _timeline(
            _shot(0.0, 20.0, "establishing_context"),
            _shot(20.0, 40.0, "metaphor"),
            _shot(40.0, 60.0, "payoff", hero=True),
            duration_seconds=60.0,
        )
        schedule = dynamics._coverage_schedule(timeline, narrative_seconds=60.0, total_seconds=64.0)
        self.assertEqual(schedule[0]["adjustment_db"], -0.8)
        self.assertEqual(schedule[1]["adjustment_db"], 0.8)
        self.assertEqual(schedule[2]["adjustment_db"], 2.0)
        self.assertEqual(schedule[-1]["intent"], "outro_quiet_tail")
        self.assertEqual(schedule[-1]["adjustment_db"], dynamics.OUTRO_ADJUSTMENT_DB)

    def test_preserved_external_authority_stays_neutral(self) -> None:
        timeline = _timeline(
            {
                "shot_id": "opening",
                "start_seconds": 0.0,
                "end_seconds": 15.0,
                "human_editorial_intent": {"status": "preserved_external_authority"},
            },
            _shot(15.0, 30.0, "emotional_reinforcement"),
            duration_seconds=30.0,
        )
        schedule = dynamics._coverage_schedule(timeline, narrative_seconds=30.0, total_seconds=30.0)
        self.assertEqual(schedule[0]["adjustment_db"], 0.0)
        self.assertEqual(schedule[0]["derivation"], "preserved_m6_or_legacy_authority")
        self.assertEqual(schedule[1]["adjustment_db"], 1.2)

    def test_duration_mismatch_fails_closed_for_bound_semantic_timeline(self) -> None:
        timeline = _timeline(_shot(0.0, 30.0, "metaphor"), duration_seconds=30.0)
        with self.assertRaisesRegex(dynamics.NarrativeMusicDynamicsError, "timeline_narration_duration_mismatch"):
            dynamics._coverage_schedule(timeline, narrative_seconds=34.0, total_seconds=34.0)

    def test_ramps_are_continuous_and_expression_is_finite(self) -> None:
        schedule = [
            {"start_seconds": 0.0, "end_seconds": 10.0, "adjustment_db": -0.8},
            {"start_seconds": 10.0, "end_seconds": 20.0, "adjustment_db": 1.8},
            {"start_seconds": 20.0, "end_seconds": 30.0, "adjustment_db": -1.8},
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

    def test_nonsemantic_timeline_is_exact_passthrough(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "visual-timeline.json").write_text(
                json.dumps({"duration_seconds": 20.0, "human_editorial_intent": {"status": "preserved_legacy_timeline"}}),
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
            self.assertEqual(calls[0][3], music)
            report = json.loads((root / "narrative-music-dynamics.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "not_applicable")

    def test_semantic_wrapper_supplies_transient_music_then_deletes_it(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            timeline = _timeline(
                _shot(0.0, 10.0, "establishing_context"),
                _shot(10.0, 20.0, "payoff"),
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

            with patch.object(dynamics.orchestrator, "mux", original), patch.object(dynamics, "duration", return_value=20.0), patch.object(dynamics, "_render_dynamic_music", side_effect=fake_render):
                dynamics.install_narrative_music_dynamics()
                result = dynamics.orchestrator.mux(video, narration, root / "final.mp4", music=music, target_lufs=-16.0)

            self.assertEqual(result, root / "final.mp4")
            self.assertEqual(seen["kwargs"]["target_lufs"], -16.0)
            self.assertEqual(seen["music"].name, "narrative-music-dynamics.wav")
            self.assertFalse((root / "narrative-music-dynamics.wav").exists())
            report = json.loads((root / "narrative-music-dynamics.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "applied")
            self.assertTrue(report["music_bed_only"])
            self.assertEqual(report["ai_calls_added"], 0)
            self.assertFalse(report["narration_authority_changed"])
            self.assertFalse(report["rights_provenance_changed"])


if __name__ == "__main__":
    unittest.main()

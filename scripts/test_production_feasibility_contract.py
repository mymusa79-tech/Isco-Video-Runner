from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import isco_video_agent.orchestrator as orchestrator
import isco_video_agent.resilient_planner as resilient_planner

from scripts import production_feasibility_contract as feasibility


class ProductionFeasibilityContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_concat_audio = orchestrator.concat_audio
        self.original_duration_limits = orchestrator._duration_limits
        self.original_duration = orchestrator.duration
        self.original_word_bounds = dict(resilient_planner._DURATION_WORD_BOUNDS)
        self.original_targets = dict(resilient_planner._TARGET_TOTAL_WORDS)

    def tearDown(self) -> None:
        orchestrator.concat_audio = self.original_concat_audio
        orchestrator._duration_limits = self.original_duration_limits
        orchestrator.duration = self.original_duration
        resilient_planner._DURATION_WORD_BOUNDS.clear()
        resilient_planner._DURATION_WORD_BOUNDS.update(self.original_word_bounds)
        resilient_planner._TARGET_TOTAL_WORDS.clear()
        resilient_planner._TARGET_TOTAL_WORDS.update(self.original_targets)

    def test_story_contract_intersects_words_with_real_media_window(self):
        self.assertEqual(feasibility.final_duration_bounds("story"), (168.0, 360.0))
        self.assertEqual(feasibility.long_word_bounds("story"), (420, 630))
        self.assertEqual(feasibility.target_words("story"), 525)

    def test_film_mature_band_is_preserved(self):
        self.assertEqual(feasibility.final_duration_bounds("film"), (300.0, 900.0))
        self.assertEqual(feasibility.long_word_bounds("film"), (800, 1450))
        self.assertEqual(feasibility.target_words("film"), 1125)

    def test_moment_family_visibility_preserves_run187_professional_floor(self):
        self.assertEqual(feasibility.final_duration_bounds("moment"), (12.0, 20.0))

    def test_install_rebinds_story_planning_and_final_qc_to_same_authority(self):
        evidence = feasibility.install_production_feasibility_contract()
        self.assertEqual(resilient_planner._DURATION_WORD_BOUNDS["story"], (420, 630))
        self.assertEqual(resilient_planner._TARGET_TOTAL_WORDS["story"], 525)
        self.assertEqual(resilient_planner._DURATION_WORD_BOUNDS["film"], (800, 1450))

        cfg = {
            "formats": {
                "film": {"target_seconds": 480},
                "story": {"target_seconds": 240},
                "moment": {"target_seconds": 15},
            }
        }
        self.assertEqual(orchestrator._duration_limits(cfg, "story"), (168.0, 360.0))
        self.assertEqual(orchestrator._duration_limits(cfg, "film"), (300.0, 900.0))
        self.assertEqual(evidence["contract_id"], feasibility.CONTRACT_ID)

    def test_underlength_story_fails_immediately_after_tts_before_visual_work(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output = root / "narration.wav"
            (root / "plan.json").write_text(
                json.dumps({"format": "story"}), encoding="utf-8"
            )

            def fake_concat(_inputs: list[Path], dest: Path) -> Path:
                Path(dest).write_bytes(b"audio")
                return Path(dest)

            orchestrator.concat_audio = fake_concat
            orchestrator.duration = lambda _path: 150.0
            feasibility.install_production_feasibility_contract()

            with self.assertRaisesRegex(
                feasibility.ProductionFeasibilityError,
                "PRODUCTION_FEASIBILITY_AUDIO_OUTSIDE_CONTRACT",
            ):
                orchestrator.concat_audio([root / "section.wav"], output)

            report = json.loads((root / "production-feasibility.json").read_text(encoding="utf-8"))
            self.assertFalse(report["accepted"])
            self.assertEqual(report["stage"], "post_tts_pre_visual")
            self.assertEqual(report["actual_narration_seconds"], 150.0)
            self.assertEqual(report["min_seconds"], 168.0)

    def test_feasible_story_returns_from_concat_and_records_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output = root / "narration.wav"
            (root / "plan.json").write_text(
                json.dumps({"format": "story"}), encoding="utf-8"
            )

            def fake_concat(_inputs: list[Path], dest: Path) -> Path:
                Path(dest).write_bytes(b"audio")
                return Path(dest)

            orchestrator.concat_audio = fake_concat
            orchestrator.duration = lambda _path: 240.0
            feasibility.install_production_feasibility_contract()
            result = orchestrator.concat_audio([root / "section.wav"], output)

            self.assertEqual(result, output)
            report = json.loads((root / "production-feasibility.json").read_text(encoding="utf-8"))
            self.assertTrue(report["accepted"])
            self.assertEqual(report["actual_narration_seconds"], 240.0)

    def test_non_production_concat_audio_call_retains_historical_behavior(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output = root / "other.wav"

            def fake_concat(_inputs: list[Path], dest: Path) -> Path:
                Path(dest).write_bytes(b"audio")
                return Path(dest)

            orchestrator.concat_audio = fake_concat
            orchestrator.duration = lambda _path: (_ for _ in ()).throw(AssertionError("must not probe"))
            feasibility.install_production_feasibility_contract()
            self.assertEqual(orchestrator.concat_audio([root / "section.wav"], output), output)


if __name__ == "__main__":
    unittest.main()

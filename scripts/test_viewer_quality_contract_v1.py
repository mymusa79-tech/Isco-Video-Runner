from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.viewer_quality_contract_v1 import enforce_viewer_quality_contract


class ViewerQualityContractV1Tests(unittest.TestCase):
    def _base_dir(self, *, visual_relevance: float = 0.90, visual_quality: float = 0.90) -> Path:
        root = Path(tempfile.mkdtemp(prefix="viewer-quality-v1-"))
        (root / "plan.json").write_text(json.dumps({"format": "moment"}), encoding="utf-8")
        (root / "final-master-qc.json").write_text(
            json.dumps(
                {
                    "status": "pass",
                    "full_decode_ok": True,
                    "blocking_findings": [],
                    "detectors": {"exact_freeze": {"events": []}},
                }
            ),
            encoding="utf-8",
        )
        (root / "quality-final.json").write_text(
            json.dumps(
                {
                    "duration_ok": True,
                    "audio_ok": True,
                    "av_sync_ok": True,
                    "video_streams": 1,
                    "audio_streams": 1,
                    "audio_measurement": {"integrated_lufs": -15.77},
                    "audio_target_lufs": -16.0,
                    "av_delta_seconds": 0.017,
                }
            ),
            encoding="utf-8",
        )
        (root / "visual-audit.json").write_text(
            json.dumps(
                [
                    {
                        "status": "pass",
                        "provider": "pexels",
                        "candidate_id": "a",
                        "relevance": visual_relevance,
                        "visual_quality": visual_quality,
                    },
                    {
                        "status": "pass",
                        "provider": "pixabay",
                        "candidate_id": "b",
                        "relevance": 0.90,
                        "visual_quality": 0.95,
                    },
                ]
            ),
            encoding="utf-8",
        )
        (root / "short-visual-timeline.json").write_text(
            json.dumps(
                {
                    "shot_count": 3,
                    "semantic_beat_count": 4,
                    "visual_density_contract": {"actual_max_shot_hold_seconds": 5.414},
                }
            ),
            encoding="utf-8",
        )
        return root

    def test_short_with_run225_class_evidence_clears_85(self) -> None:
        root = self._base_dir(visual_relevance=0.85, visual_quality=0.90)
        report = enforce_viewer_quality_contract(
            root,
            fmt="moment",
            critic={"status": "pass", "hard_blocks": []},
        )
        self.assertEqual(report["verdict"], "pass")
        self.assertGreaterEqual(report["viewer_score_10"], 8.5)
        self.assertEqual(report["provider_calls_added"], 0)
        self.assertEqual(report["acceptance_phase"], "pre_state_acceptance")
        self.assertEqual(report["release_profile"], "standalone_short")

    def test_weak_visual_semantics_cannot_be_hidden_by_technical_pass(self) -> None:
        root = self._base_dir(visual_relevance=0.79, visual_quality=1.0)
        with self.assertRaisesRegex(RuntimeError, "Viewer Quality Contract V1 blocked release"):
            enforce_viewer_quality_contract(
                root,
                fmt="moment",
                critic={"status": "pass", "hard_blocks": []},
            )
        report = json.loads((root / "viewer-quality-contract.json").read_text(encoding="utf-8"))
        self.assertEqual(report["verdict"], "block")
        self.assertFalse(report["non_compensable_gates"]["visual_semantics"])

    def test_long_uses_film_profile_without_short_timeline(self) -> None:
        root = self._base_dir()
        (root / "plan.json").write_text(json.dumps({"format": "film"}), encoding="utf-8")
        (root / "short-visual-timeline.json").unlink()
        report = enforce_viewer_quality_contract(
            root,
            fmt="film",
            critic={"status": "pass", "hard_blocks": []},
        )
        self.assertEqual(report["verdict"], "pass")
        self.assertEqual(report["release_profile"], "film")
        self.assertEqual(report["dimensions"]["pacing"]["format_class"], "long")

    def test_derived_short_gets_distinct_release_profile(self) -> None:
        root = self._base_dir()
        (root / "plan.json").write_text(
            json.dumps(
                {
                    "format": "moment",
                    "plan_source": "source_derived_long_episode_video_short",
                }
            ),
            encoding="utf-8",
        )
        report = enforce_viewer_quality_contract(
            root,
            fmt="moment",
            critic={"status": "pass", "hard_blocks": []},
        )
        self.assertEqual(report["release_profile"], "derived_short")

    def test_gold_critic_remains_non_compensable_before_state_acceptance(self) -> None:
        root = self._base_dir()
        with self.assertRaisesRegex(RuntimeError, "Viewer Quality Contract V1 blocked release"):
            enforce_viewer_quality_contract(
                root,
                fmt="story",
                critic={"status": "block", "hard_blocks": ["editorial"]},
            )
        report = json.loads((root / "viewer-quality-contract.json").read_text(encoding="utf-8"))
        self.assertFalse(report["non_compensable_gates"]["gold"])


if __name__ == "__main__":
    unittest.main()

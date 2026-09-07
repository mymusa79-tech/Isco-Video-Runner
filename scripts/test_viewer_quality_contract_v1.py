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
                        "is_selected": True,
                        "section": "s1",
                        "provider": "pexels",
                        "candidate_id": "a",
                        "relevance": visual_relevance,
                        "visual_quality": visual_quality,
                    },
                    {
                        "status": "pass",
                        "is_final_cut_auxiliary": True,
                        "section": "s1",
                        "opening_slot": "cold_open",
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
        self.assertEqual(
            report["dimensions"]["visual_semantics"]["coverage_semantics"],
            "canonical_final_cut_selected_audits_only_v2",
        )

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

    def test_historical_pass_candidate_cannot_inflate_final_cut_score(self) -> None:
        root = self._base_dir(visual_relevance=0.79, visual_quality=1.0)
        audits = json.loads((root / "visual-audit.json").read_text(encoding="utf-8"))
        audits.append(
            {
                "status": "pass",
                "is_selected": False,
                "section": "s1",
                "candidate_id": "historical-perfect",
                "relevance": 1.0,
                "visual_quality": 1.0,
            }
        )
        (root / "visual-audit.json").write_text(json.dumps(audits), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "Viewer Quality Contract V1 blocked release"):
            enforce_viewer_quality_contract(
                root,
                fmt="moment",
                critic={"status": "pass", "hard_blocks": []},
            )
        report = json.loads((root / "viewer-quality-contract.json").read_text(encoding="utf-8"))
        self.assertEqual(report["dimensions"]["visual_semantics"]["final_cut_records"], 2)
        self.assertEqual(report["dimensions"]["visual_semantics"]["semantic_min"], 0.79)

    def test_long_sequence_composite_uses_member_floor_not_historical_candidates(self) -> None:
        root = self._base_dir()
        (root / "plan.json").write_text(json.dumps({"format": "film"}), encoding="utf-8")
        (root / "short-visual-timeline.json").unlink()
        (root / "visual-audit.json").write_text(
            json.dumps(
                [
                    {
                        "status": "pass",
                        "is_selected": False,
                        "is_section_sequence_member": True,
                        "section": "s1",
                        "candidate_id": "member-a",
                        "relevance": 0.99,
                        "visual_quality": 0.99,
                    },
                    {
                        "status": "pass",
                        "is_selected": False,
                        "is_section_sequence_member": True,
                        "section": "s1",
                        "candidate_id": "member-b",
                        "relevance": 0.86,
                        "visual_quality": 0.91,
                    },
                    {
                        "status": "pass",
                        "is_selected": True,
                        "is_section_sequence": True,
                        "sequence_member_count": 2,
                        "section": "s1",
                        "candidate_id": "sequence-composite",
                        "relevance": 0.86,
                        "visual_quality": 0.91,
                    },
                ]
            ),
            encoding="utf-8",
        )
        report = enforce_viewer_quality_contract(
            root,
            fmt="film",
            critic={"status": "pass", "hard_blocks": []},
        )
        visual = report["dimensions"]["visual_semantics"]
        self.assertEqual(visual["final_cut_records"], 1)
        self.assertEqual(visual["semantic_min"], 0.86)
        self.assertGreaterEqual(report["viewer_score_10"], 8.5)

    def test_missing_final_cut_selection_fails_closed(self) -> None:
        root = self._base_dir()
        audits = json.loads((root / "visual-audit.json").read_text(encoding="utf-8"))
        for audit in audits:
            audit.pop("is_selected", None)
            audit.pop("is_final_cut_auxiliary", None)
        (root / "visual-audit.json").write_text(json.dumps(audits), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "final-cut visual-audit evidence"):
            enforce_viewer_quality_contract(
                root,
                fmt="moment",
                critic={"status": "pass", "hard_blocks": []},
            )

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
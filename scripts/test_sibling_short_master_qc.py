from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.sibling_short_orchestration import stage_sibling_assets, validate_completed_short


def _json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _request() -> dict:
    return {
        "request_id": "parent-s1",
        "request_sha256": "a" * 64,
        "approved_topic": "زاوية مستقلة",
        "source_semantic_job": "زاوية مستقلة",
        "source_sibling_plan_sha256": "b" * 64,
        "source_production_plan_sha256": "c" * 64,
        "source_episode_excerpt": {"source_section_id": "s2"},
    }


def _passing_qc() -> dict:
    return {
        "status": "pass",
        "production_stage": "post_render_pre_gold_acceptance",
        "full_decode_ok": True,
        "final_media_mutated": False,
        "blocking_findings": [],
    }


def _completed_root(td: str) -> Path:
    root = Path(td)
    (root / "final.mp4").write_bytes(b"x" * 2048)
    _json(root / "quality-final.json", {"format": "moment", "duration_ok": True, "duration_seconds": 12.0})
    _json(root / "short-intelligence.json", {"delivery_allowed": True, "request_id": "parent-s1"})
    _json(
        root / "gold-enforce-report.json",
        {"phase": "4", "mode": "enforce", "gold": {"accepted": True}, "same_render": {"artifact_divergence": False}},
    )
    _json(root / "rights-manifest.json", {"assets": [{"ok": True}]})
    _json(root / "plan.json", {"format": "moment", "topic": "زاوية مستقلة"})
    _json(root / "final-master-qc.json", _passing_qc())
    return root


class SiblingShortMasterQCTests(unittest.TestCase):
    def test_completed_short_requires_passing_master_qc(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _completed_root(td)
            result = validate_completed_short(root, _request())
            self.assertTrue(result["delivery_allowed"])
            self.assertEqual(Path(result["final_master_qc"]).name, "final-master-qc.json")

    def test_missing_or_blocked_master_qc_rejects_short(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _completed_root(td)
            (root / "final-master-qc.json").unlink()
            with self.assertRaisesRegex(RuntimeError, "Invalid JSON artifact: final-master-qc.json"):
                validate_completed_short(root, _request())
        with tempfile.TemporaryDirectory() as td:
            root = _completed_root(td)
            _json(root / "final-master-qc.json", {**_passing_qc(), "status": "block", "blocking_findings": ["full_decode_failed"]})
            with self.assertRaisesRegex(RuntimeError, "failed Final Master QC"):
                validate_completed_short(root, _request())

    def test_staging_copies_master_qc_with_each_short(self) -> None:
        with tempfile.TemporaryDirectory() as child_td, tempfile.TemporaryDirectory() as parent_td:
            child = _completed_root(child_td)
            completed = validate_completed_short(child, _request())
            staged = stage_sibling_assets(Path(parent_td), [completed, {**completed, "semantic_job": "زاوية ثانية", "request_id": "parent-s2"}])
            self.assertEqual(len(staged), 2)
            for index, item in enumerate(staged, 1):
                qc_name = f"short-{index:02d}-master-qc.json"
                self.assertEqual(item["final_master_qc"], qc_name)
                self.assertTrue((Path(parent_td) / qc_name).is_file())
                qc = json.loads((Path(parent_td) / qc_name).read_text(encoding="utf-8"))
                self.assertEqual(qc["status"], "pass")


if __name__ == "__main__":
    unittest.main()

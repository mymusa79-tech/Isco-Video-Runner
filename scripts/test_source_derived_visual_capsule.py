from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import source_derived_visual_capsule as capsule


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _timeline() -> dict:
    shots = [
        {
            "section_id": "s1",
            "shot_id": "sh1",
            "start_seconds": 0.0,
            "end_seconds": 8.0,
            "continuity_role": "establish",
            "visual_job": "establish context",
            "story_job": "setup",
            "cut_reason": "episode_start",
            "selected_asset": {"candidate_ref": "p1", "provider": "pexels", "asset_id": 11, "source_url": "https://pexels.example/11"},
            "final_cut_audit_reference": {"file": "candidate_manifest.json", "json_pointer": "/scenes/0/candidates/0"},
            "rights_reference": {"provider": "pexels", "asset_id": 11},
        },
        {
            "section_id": "s1",
            "shot_id": "sh2",
            "start_seconds": 8.0,
            "end_seconds": 16.0,
            "continuity_role": "contrast",
            "visual_job": "turn",
            "story_job": "reframe",
            "cut_reason": "semantic_scene_boundary",
            "selected_asset": {"candidate_ref": "p2", "provider": "pixabay", "asset_id": 22, "source_url": "https://pixabay.example/22"},
            "final_cut_audit_reference": {"file": "candidate_manifest.json", "json_pointer": "/scenes/1/candidates/0"},
            "rights_reference": {"provider": "pixabay", "asset_id": 22},
        },
        {
            "section_id": "s1",
            "shot_id": "sh3",
            "start_seconds": 16.0,
            "end_seconds": 26.0,
            "continuity_role": "payoff",
            "visual_job": "release",
            "story_job": "payoff",
            "cut_reason": "semantic_scene_boundary",
            "selected_asset": {"candidate_ref": "p3", "provider": "pexels", "asset_id": 33, "source_url": "https://pexels.example/33"},
            "final_cut_audit_reference": {"file": "candidate_manifest.json", "json_pointer": "/scenes/2/candidates/0"},
            "rights_reference": {"provider": "pexels", "asset_id": 33},
        },
    ]
    return {
        "schema_version": 1,
        "status": "ok",
        "timeline_mode": "semantic_director",
        "cut_policy": {"semantic_cuts_only": True, "automatic_every_n_seconds": False},
        "sections": [{"section_id": "s1", "start_seconds": 0.0, "end_seconds": 26.0, "beats": [{}]}],
        "final_cut_visuals": shots,
    }


def _rights() -> dict:
    def item(provider: str, asset_id: int) -> dict:
        return {
            "provider": provider,
            "asset_id": asset_id,
            "source_url": f"https://{provider}.example/{asset_id}",
            "license_url": "https://license.example/",
            "retrieved_at": "2026-09-07T00:00:00Z",
            "rights_flags": {},
        }
    return {"visuals": [item("pexels", 11), item("pixabay", 22), item("pexels", 33)], "music": None}


def _parent(root: Path) -> None:
    (root / "picture.mp4").write_bytes(b"p" * 4096)
    (root / "final.mp4").write_bytes(b"f" * 4096)
    _write_json(root / "visual-timeline.json", _timeline())
    _write_json(root / "rights-manifest.json", _rights())


class SourceDerivedVisualCapsuleTests(unittest.TestCase):
    def test_capsule_binds_exact_parent_video_timeline_and_rights(self) -> None:
        with tempfile.TemporaryDirectory() as td, mock.patch.object(capsule, "duration", return_value=26.0):
            root = Path(td)
            _parent(root)
            value = capsule.build_parent_visual_capsule(root, "s1")
            self.assertEqual(value["source_section_id"], "s1")
            self.assertEqual(value["source_duration_seconds"], 26.0)
            self.assertEqual(len(value["shots"]), 3)
            self.assertTrue(value["inheritance_policy"]["actual_parent_video_required"])
            self.assertTrue(value["inheritance_policy"]["textual_visual_query_is_not_video_authority"])
            self.assertFalse(value["inheritance_policy"]["new_stock_search_allowed"])
            self.assertFalse(value["inheritance_policy"]["new_vision_review_allowed"])
            self.assertEqual(capsule.validate_parent_visual_capsule(root, value)["capsule_sha256"], value["capsule_sha256"])

    def test_parent_picture_tamper_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td, mock.patch.object(capsule, "duration", return_value=26.0):
            root = Path(td)
            _parent(root)
            value = capsule.build_parent_visual_capsule(root, "s1")
            (root / "picture.mp4").write_bytes(b"changed" * 1024)
            with self.assertRaisesRegex(capsule.SourceDerivedVisualCapsuleError, "parent_bytes_changed:picture.mp4"):
                capsule.validate_parent_visual_capsule(root, value)

    def test_capsule_document_cannot_restore_visual_query_as_authority(self) -> None:
        with tempfile.TemporaryDirectory() as td, mock.patch.object(capsule, "duration", return_value=26.0):
            root = Path(td)
            _parent(root)
            value = capsule.build_parent_visual_capsule(root, "s1")
            value["inheritance_policy"]["textual_visual_query_is_not_video_authority"] = False
            value["capsule_sha256"] = capsule._canonical_hash(value)
            with self.assertRaisesRegex(capsule.SourceDerivedVisualCapsuleError, "query_authority_regression"):
                capsule.validate_capsule_document(value)

    def test_representative_parent_selection_never_exceeds_four_and_keeps_order(self) -> None:
        shots = []
        for index in range(8):
            shots.append(
                {
                    "shot_id": f"sh{index}",
                    "start_seconds": float(index * 5),
                    "end_seconds": float((index + 1) * 5),
                    "continuity_role": "contrast" if index == 4 else "develop",
                }
            )
        selected = capsule._representative_shots(shots, 18.0)
        self.assertLessEqual(len(selected), 4)
        starts = [float(item["start_seconds"]) for item in selected]
        self.assertEqual(starts, sorted(starts))
        self.assertEqual(selected[0]["shot_id"], "sh0")
        self.assertEqual(selected[-1]["shot_id"], "sh7")

    def test_child_rights_are_replaced_by_actual_used_parent_assets(self) -> None:
        with tempfile.TemporaryDirectory() as td, mock.patch.object(capsule, "duration", return_value=26.0):
            root = Path(td)
            parent = root / "parent"
            child = root / "child"
            parent.mkdir()
            child.mkdir()
            _parent(parent)
            value = capsule.build_parent_visual_capsule(parent, "s1")
            _write_json(child / "rights-manifest.json", {"visuals": [{"provider": "pexels", "asset_id": 999}]})
            _write_json(child / "quality-final.json", {"visual_sections_reviewed": 1})
            report = {
                "used_parent_shots": [
                    {
                        "shot_id": "sh2",
                        "selected_asset": {"provider": "pixabay", "asset_id": 22},
                        "final_cut_audit_reference": {"file": "candidate_manifest.json", "json_pointer": "/scenes/1/candidates/0"},
                    }
                ]
            }
            result = capsule.inherit_parent_visual_evidence(parent, value, child, report)
            rights = json.loads((child / "rights-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual([(x["provider"], x["asset_id"]) for x in rights["visuals"]], [("pixabay", 22)])
            self.assertTrue(rights["source_derived_parent_visual_v1"]["actual_parent_video_inherited"])
            self.assertEqual(result["inherited_visual_rights"], 1)
            audit = json.loads((child / "visual-audit.json").read_text(encoding="utf-8"))
            self.assertEqual(audit[0]["review_origin"], "inherited_parent_m7_final_cut")
            self.assertFalse(audit[0]["new_vision_review_performed"])


if __name__ == "__main__":
    unittest.main()
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.unified_delivery import build_delivery_manifest, finalize_release_manifest


def _json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class UnifiedDeliveryCanonicalTests(unittest.TestCase):
    def _root(self, td: str) -> Path:
        root=Path(td)
        _json(root/"plan.json", {"format":"film","topic":"موضوع"})
        _json(root/"quality-final.json", {"format":"film"})
        _json(root/"production-manifest.json", {"format":"film"})
        _json(root/"final-master-qc.json", {"status":"pass","production_stage":"post_render_pre_gold_acceptance","full_decode_ok":True,"blocking_findings":[]})
        (root/"final.mp4").write_bytes(b"video")
        candidates=[]
        for i in range(1,4):
            name=f"thumbnail-{i}.jpg"; (root/name).write_bytes(b"image")
            candidates.append({"file":name,"experiment_slot":chr(64+i),"title_ar":f"عنوان {i}","text_ar":f"نص {i}"})
        _json(root/"thumbnail-plan.json", {"candidates":candidates})
        _json(root/"rights-manifest.json", {"ok":True})
        _json(root/"gold-enforce-report.json", {"ok":True})
        for name in (
            "canonical-bundle-request.json","sibling-short-plan.json","sibling-short-results.json",
            "visual-timeline.json","audio-mastering.json","sfx-plan.json","m9-transitions.json",
            "m10-cards.json","m11-report.json","cta-plan.json",
        ):
            _json(root/name,{"status":"ok"})
        _json(
            root/"narrative-music-dynamics.json",
            {"status":"applied","mode":"m7_adaptive_pacing_music_dynamics","segments":[{"start_seconds":0.0,"end_seconds":10.0,"adjustment_db":-0.8}]},
        )
        _json(root/"clip-1.m8.json", {"status":"applied"})
        return root

    def test_long_plus_shorts_manifest_is_one_manual_nonpartial_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root=self._root(td)
            shorts=[]
            for i in range(1,3):
                video=f"short-{i:02d}.mp4"; (root/video).write_bytes(b"short")
                shorts.append({"semantic_job":f"زاوية {i}","video":video,"delivery_allowed":True})
            request={
                "request_id":"canonical-x","request_sha256":"a"*64,"source":"canonical_v4_approved_brief",
                "approval_scope":"long_plus_sibling_shorts","approved_topic":"موضوع","approved_at":"now",
                "parent_approved_brief_sha256":"b"*64,
            }
            manifest=build_delivery_manifest(root,repository="mymusa79-tech/Isco-Video-Runner",release_tag=None,request=request,short_assets=shorts)
            self.assertEqual(manifest["delivery_kind"],"long_plus_shorts")
            self.assertEqual(manifest["short_count"],2)
            self.assertEqual(len(manifest["title_thumbnail_pairs"]),3)
            self.assertEqual(manifest["youtube_publish_mode"],"manual_in_youtube_studio")
            self.assertFalse(manifest["publication_performed"])
            self.assertFalse(manifest["partial_delivery_allowed"])
            self.assertEqual(manifest["control_request"]["source"],"canonical_v4_approved_brief")
            self.assertEqual(manifest["cinematic_reports"]["m11_archive"],"m11-report.json")
            self.assertEqual(manifest["cinematic_reports"]["contextual_cta"],"cta-plan.json")
            dynamics=manifest["cinematic_reports"]["narrative_music_dynamics"]
            self.assertEqual(dynamics["file"],"narrative-music-dynamics.json")
            self.assertEqual(dynamics["evidence"]["status"],"applied")
            self.assertEqual(dynamics["evidence"]["segments"][0]["adjustment_db"],-0.8)
            master_qc=manifest["final_master_qc"]
            self.assertEqual(master_qc["file"],"final-master-qc.json")
            self.assertEqual(master_qc["evidence"]["status"],"pass")
            self.assertTrue(master_qc["evidence"]["full_decode_ok"])
            self.assertEqual(manifest["cinematic_reports"]["m8_color_normalization"],["clip-1.m8.json"])
            self.assertEqual(manifest["canonical_bundle_request"],"canonical-bundle-request.json")

    def test_missing_or_blocked_master_qc_cannot_enter_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root=self._root(td)
            (root/"final-master-qc.json").unlink()
            with self.assertRaises(RuntimeError):
                build_delivery_manifest(root,repository="r/x",release_tag=None)
        with tempfile.TemporaryDirectory() as td:
            root=self._root(td)
            _json(root/"final-master-qc.json", {"status":"block","blocking_findings":["full_decode_failed"]})
            with self.assertRaises(RuntimeError):
                build_delivery_manifest(root,repository="r/x",release_tag=None)

    def test_partial_or_duplicate_short_set_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root=self._root(td)
            (root/"short-01.mp4").write_bytes(b"s")
            request={"approval_scope":"long_plus_sibling_shorts"}
            with self.assertRaises(RuntimeError):
                build_delivery_manifest(root,repository="r/x",release_tag=None,request=request,short_assets=[{"semantic_job":"x","video":"short-01.mp4","delivery_allowed":True}])

    def test_release_finalization_never_claims_youtube_publication(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/"delivery-manifest.json"
            _json(path,{"release_state":"staged","youtube_publish_mode":"manual_in_youtube_studio","publication_performed":False})
            finalize_release_manifest(path,repository="mymusa79-tech/Isco-Video-Runner",release_tag="video-123")
            manifest=json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["release_state"],"released")
            self.assertEqual(manifest["release_tag"],"video-123")
            self.assertFalse(manifest["publication_performed"])
            self.assertEqual(manifest["youtube_publish_mode"],"manual_in_youtube_studio")


if __name__ == "__main__": unittest.main()

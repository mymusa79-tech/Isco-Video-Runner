from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.unified_delivery import build_delivery_manifest


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class UnifiedDeliveryDeferredChildrenTests(unittest.TestCase):
    def _root(self) -> Path:
        root = Path(tempfile.mkdtemp(prefix="deferred-delivery-"))
        (root / "final.mp4").write_bytes(b"accepted-parent")
        (root / "plan.json").write_text(json.dumps({"topic": "topic", "format": "film"}), encoding="utf-8")
        (root / "quality-final.json").write_text(json.dumps({"format": "film"}), encoding="utf-8")
        (root / "production-manifest.json").write_text(json.dumps({"format": "film"}), encoding="utf-8")
        (root / "final-master-qc.json").write_text('{"status":"pass"}', encoding="utf-8")
        (root / "master-lock.json").write_text('{"state":"locked_after_gold"}', encoding="utf-8")
        candidates = []
        for index, slot in enumerate(("A", "B", "C"), 1):
            name = f"thumbnail-{index}.jpg"
            (root / name).write_bytes(b"j" * 2048)
            candidates.append(
                {
                    "experiment_slot": slot,
                    "title_ar": f"title {index}",
                    "file": name,
                    "text_ar": f"text {index}",
                    "packaging_hypothesis": f"hypothesis {index}",
                }
            )
        (root / "thumbnail-plan.json").write_text(json.dumps({"candidates": candidates}), encoding="utf-8")
        return root

    def _request(self) -> dict:
        return {
            "request_id": "req-parent",
            "request_sha256": "request-sha",
            "approval_scope": "long_plus_sibling_shorts",
            "approved_topic": "topic",
        }

    def _acceptance(self, root: Path) -> dict:
        return {
            "status": "pass",
            "acceptance_contract": {
                "sources": {"final": {"sha256": _sha(root / "final.mp4")}}
            },
        }

    def _write_deferred(self, root: Path) -> None:
        (root / "sibling-short-deferred.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "contract_id": "post_gold.sibling_short_deferred.v1",
                    "status": "deferred_after_parent_gold",
                    "parent_request_id": "req-parent",
                    "parent_request_sha256": "request-sha",
                    "automatic_production_started": False,
                    "provider_calls_performed": False,
                    "blocking_parent_delivery": False,
                    "execution_owner": "isolated_child_jobs",
                    "sibling_short_plan": {"file": "sibling-short-plan.json", "sha256": "abc", "short_count": 3},
                }
            ),
            encoding="utf-8",
        )

    def test_locked_parent_can_stage_while_children_are_explicitly_deferred(self) -> None:
        root = self._root()
        self._write_deferred(root)
        with patch("scripts.unified_delivery.require_final_master_acceptance", return_value=self._acceptance(root)):
            manifest = build_delivery_manifest(
                root,
                repository="mymusa79-tech/Isco-Video-Runner",
                release_tag="video-1",
                request=self._request(),
                short_assets=[],
            )
        self.assertEqual(manifest["delivery_kind"], "long")
        self.assertEqual(manifest["requested_delivery_kind"], "long_plus_shorts")
        self.assertEqual(manifest["short_count"], 0)
        self.assertTrue(manifest["parent_delivery_independent_of_derived_assets"])
        self.assertEqual(manifest["sibling_short_continuation"]["status"], "deferred_after_parent_gold")
        self.assertFalse(manifest["sibling_short_continuation"]["blocking_parent_delivery"])
        self.assertIsNotNone(manifest["master_lock"])

    def test_parent_only_staging_without_deferred_contract_fails_closed(self) -> None:
        root = self._root()
        with patch("scripts.unified_delivery.require_final_master_acceptance", return_value=self._acceptance(root)):
            with self.assertRaisesRegex(RuntimeError, "explicit deferred child contract"):
                build_delivery_manifest(
                    root,
                    repository="mymusa79-tech/Isco-Video-Runner",
                    release_tag="video-1",
                    request=self._request(),
                    short_assets=[],
                )

    def test_deferred_contract_bound_to_wrong_request_fails_closed(self) -> None:
        root = self._root()
        self._write_deferred(root)
        document = json.loads((root / "sibling-short-deferred.json").read_text(encoding="utf-8"))
        document["parent_request_id"] = "other-request"
        (root / "sibling-short-deferred.json").write_text(json.dumps(document), encoding="utf-8")
        with patch("scripts.unified_delivery.require_final_master_acceptance", return_value=self._acceptance(root)):
            with self.assertRaisesRegex(RuntimeError, "not bound"):
                build_delivery_manifest(
                    root,
                    repository="mymusa79-tech/Isco-Video-Runner",
                    release_tag="video-1",
                    request=self._request(),
                    short_assets=[],
                )


if __name__ == "__main__":
    unittest.main()

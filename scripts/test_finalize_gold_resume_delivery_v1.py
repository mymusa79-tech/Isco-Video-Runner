from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import finalize_gold_resume_delivery_v1 as resume_delivery


RUNNER_SHA = "c" * 40
ENGINE_SHA = "d" * 40
SOURCE_RUNNER_SHA = "a" * 40
SOURCE_ENGINE_SHA = "b" * 40


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class FinalizeGoldResumeDeliveryV1Tests(unittest.TestCase):
    def _root(self, *, fmt: str) -> Path:
        root = Path(tempfile.mkdtemp(prefix="post-gold-delivery-"))
        (root / "final.mp4").write_bytes(b"immutable-parent-final")
        (root / "plan.json").write_text(
            json.dumps(
                {
                    "format": fmt,
                    "topic": "topic",
                    "sections": [
                        {"key_point": "one"},
                        {"key_point": "two"},
                        {"key_point": "three"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        (root / "gold-enforce-report.json").write_text(
            json.dumps(
                {
                    "phase": "4",
                    "mode": "enforce",
                    "gold": {"accepted": True},
                    "viewer_quality": {"verdict": "pass", "score_10": 9.0},
                    "same_render": {"artifact_divergence": False},
                }
            ),
            encoding="utf-8",
        )
        return root

    def _request(self, *, kind: str, scope: str, fmt: str) -> dict:
        return {
            "schema_version": 1,
            "request_id": "req-test",
            "request_sha256": "x",
            "source": "telegram_editorial_control_panel",
            "kind": kind,
            "approval_scope": scope,
            "approved_by_user": True,
            "approved_topic": "topic",
            "format": fmt,
            "production_dispatch_authorized": False,
            "status": "approved_waiting_production_activation",
            "sibling_shorts": {"minimum": 2, "maximum": 3},
        }

    def _paths(self, root: Path, request: dict):
        request_path = root.parent / "request.json"
        request_path.write_text(json.dumps(request), encoding="utf-8")
        resume_manifest = root.parent / "resume-manifest.json"
        resume_manifest.write_text(
            json.dumps(
                {
                    "source": {
                        "run_id": "99123",
                        "run_attempt": "1",
                        "runner_sha": SOURCE_RUNNER_SHA,
                        "engine_sha": SOURCE_ENGINE_SHA,
                    },
                    "final_sha256": _sha(root / "final.mp4"),
                }
            ),
            encoding="utf-8",
        )
        return request_path, resume_manifest, root.parent / "result.json"

    def _common_patches(self, root: Path):
        def fake_manifest(out, *, production_id, fmt):
            document = {
                "production_id": production_id,
                "format": fmt,
                "final_sha256": _sha(Path(out) / "final.mp4"),
                "release_authority": "gold_enforced",
                "release_tag": os.environ.get("ISCO_RELEASE_TAG_OVERRIDE"),
            }
            (Path(out) / "production-manifest.json").write_text(json.dumps(document), encoding="utf-8")
            return document

        def fake_delivery(out, **kwargs):
            request = kwargs.get("request") or {}
            kind = "short" if request.get("kind") == "short" else "long"
            path = Path(out) / "delivery-manifest.json"
            path.write_text(json.dumps({"delivery_kind": kind}), encoding="utf-8")
            return path

        return (
            patch.object(resume_delivery, "validate_control_request", side_effect=lambda request, expected: request),
            patch.object(resume_delivery, "run_post_gold_observers", return_value={"decision": "pass"}),
            patch.object(resume_delivery.production, "_write_production_manifest", side_effect=fake_manifest),
            patch.object(resume_delivery, "write_delivery_manifest", side_effect=fake_delivery),
        )

    def test_resume_manifest_preserves_source_production_identity(self) -> None:
        root = self._root(fmt="film")
        source = {
            "run_id": "99123",
            "run_attempt": "2",
            "runner_sha": SOURCE_RUNNER_SHA,
            "engine_sha": SOURCE_ENGINE_SHA,
        }
        env = {
            "GITHUB_RUN_ID": "77777",
            "GITHUB_RUN_NUMBER": "333",
            "GITHUB_RUN_ATTEMPT": "4",
            "GITHUB_SHA": RUNNER_SHA,
            "ISCO_ENGINE_SHA": ENGINE_SHA,
        }
        patches = self._common_patches(root)
        with patch.dict(os.environ, env, clear=False), patches[2]:
            manifest = resume_delivery._write_resume_production_manifest(
                root,
                fmt="film",
                release_tag="video-225",
                source=source,
            )

        self.assertEqual(manifest["production_id"], "v4:99123:2")
        self.assertEqual(manifest["github_run_id"], "99123")
        self.assertIsNone(manifest["github_run_number"])
        self.assertEqual(manifest["github_run_attempt"], "2")
        self.assertEqual(manifest["runner_sha"], SOURCE_RUNNER_SHA)
        self.assertEqual(manifest["engine_sha"], SOURCE_ENGINE_SHA)
        self.assertEqual(manifest["release_tag"], "video-225")
        self.assertEqual(manifest["final_sha256"], _sha(root / "final.mp4"))
        self.assertEqual(manifest["resume_source"]["run_id"], "99123")
        self.assertEqual(manifest["resume_execution"]["run_id"], "77777")
        self.assertFalse(manifest["resume_execution"]["parent_media_rebuilt"])

    def test_standalone_short_finishes_quality_without_parent_lock(self) -> None:
        root = self._root(fmt="moment")
        (root / "short-intelligence-pre-gold.json").write_text(json.dumps({"stage": "pre_gold"}), encoding="utf-8")
        request = self._request(kind="short", scope="short_only", fmt="moment")
        request_path, manifest_path, result_path = self._paths(root, request)
        before = _sha(root / "final.mp4")
        patches = self._common_patches(root)
        with patch.dict(os.environ, {"GITHUB_SHA": RUNNER_SHA, "ISCO_ENGINE_SHA": ENGINE_SHA}, clear=False), patches[0], patches[1], patches[2], patches[3], patch.object(
            resume_delivery, "finalize_short_quality", return_value={"delivery_allowed": True}
        ) as short_final, patch.object(resume_delivery, "write_master_lock") as master_lock:
            result = resume_delivery.finalize_after_gold_resume(
                output_dir=root,
                request_path=request_path,
                resume_manifest_path=manifest_path,
                release_tag="short-225",
                runtime_root=root.parent / "runtime",
                result_output=result_path,
            )
        self.assertEqual(_sha(root / "final.mp4"), before)
        short_final.assert_called_once()
        master_lock.assert_not_called()
        self.assertFalse(result["parent_media_rebuilt"])
        self.assertEqual(result["delivery_kind"], "short")

    def test_long_plus_shorts_locks_parent_and_defers_children_without_provider_work(self) -> None:
        root = self._root(fmt="film")
        request = self._request(kind="long", scope="long_plus_sibling_shorts", fmt="film")
        request_path, manifest_path, result_path = self._paths(root, request)
        before = _sha(root / "final.mp4")
        deferred = root / "sibling-short-deferred.json"
        deferred.write_text("{}", encoding="utf-8")
        patches = self._common_patches(root)
        with patches[0], patches[1], patches[2], patches[3], patch.object(
            resume_delivery, "write_master_lock", return_value=root / "master-lock.json"
        ) as master_lock, patch.object(
            resume_delivery, "assert_master_lock", return_value={"state": "locked_after_gold"}
        ) as assert_lock, patch.object(
            resume_delivery, "_defer_approved_sibling_shorts", return_value=deferred
        ) as defer:
            result = resume_delivery.finalize_after_gold_resume(
                output_dir=root,
                request_path=request_path,
                resume_manifest_path=manifest_path,
                release_tag="video-225",
                runtime_root=root.parent / "runtime",
                result_output=result_path,
            )
        self.assertEqual(_sha(root / "final.mp4"), before)
        master_lock.assert_called_once()
        assert_lock.assert_called_once()
        defer.assert_called_once()
        self.assertEqual(result["sibling_shorts_produced_after_parent_gold"], 0)
        self.assertEqual(result["sibling_short_continuation"], str(deferred))
        self.assertEqual(result["delivery_kind"], "long")
        self.assertFalse(result["parent_media_rebuilt"])

    def test_long_only_locks_parent_without_sibling_continuation(self) -> None:
        root = self._root(fmt="film")
        request = self._request(kind="long", scope="long_only", fmt="film")
        request_path, manifest_path, result_path = self._paths(root, request)
        patches = self._common_patches(root)
        with patches[0], patches[1], patches[2], patches[3], patch.object(
            resume_delivery, "write_master_lock", return_value=root / "master-lock.json"
        ) as master_lock, patch.object(
            resume_delivery, "assert_master_lock", return_value={"state": "locked_after_gold"}
        ), patch.object(resume_delivery, "_defer_approved_sibling_shorts") as defer:
            result = resume_delivery.finalize_after_gold_resume(
                output_dir=root,
                request_path=request_path,
                resume_manifest_path=manifest_path,
                release_tag="video-225",
                runtime_root=root.parent / "runtime",
                result_output=result_path,
            )
        master_lock.assert_called_once()
        defer.assert_not_called()
        self.assertIsNone(result["sibling_short_continuation"])
        self.assertEqual(result["delivery_kind"], "long")


if __name__ == "__main__":
    unittest.main()

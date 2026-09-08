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


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class FinalizeGoldResumeDeliveryV1Tests(unittest.TestCase):
    def _root(self, *, fmt: str) -> Path:
        root = Path(tempfile.mkdtemp(prefix="post-gold-delivery-"))
        (root / "final.mp4").write_bytes(b"immutable-parent-final")
        (root / "plan.json").write_text(json.dumps({"format": fmt, "topic": "topic"}), encoding="utf-8")
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
        request = {
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
        }
        return request

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
                        "runner_sha": "a" * 40,
                        "engine_sha": "b" * 40,
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
            }
            (Path(out) / "production-manifest.json").write_text(json.dumps(document), encoding="utf-8")
            return document

        def fake_delivery(out, **kwargs):
            short_assets = list(kwargs.get("short_assets") or [])
            request = kwargs.get("request") or {}
            kind = "long_plus_shorts" if short_assets else ("short" if request.get("kind") == "short" else "long")
            path = Path(out) / "delivery-manifest.json"
            path.write_text(json.dumps({"delivery_kind": kind}), encoding="utf-8")
            return path

        return (
            patch.object(resume_delivery, "validate_control_request", side_effect=lambda request, expected: request),
            patch.object(resume_delivery, "run_post_gold_observers", return_value={"decision": "pass"}),
            patch.object(resume_delivery.production, "_write_production_manifest", side_effect=fake_manifest),
            patch.object(resume_delivery, "write_delivery_manifest", side_effect=fake_delivery),
        )

    def test_standalone_short_finishes_quality_without_parent_media_rebuild(self) -> None:
        root = self._root(fmt="moment")
        (root / "short-intelligence-pre-gold.json").write_text(json.dumps({"stage": "pre_gold"}), encoding="utf-8")
        request = self._request(kind="short", scope="short_only", fmt="moment")
        request_path, manifest_path, result_path = self._paths(root, request)
        before = _sha(root / "final.mp4")
        patches = self._common_patches(root)
        with patch.dict(os.environ, {"GITHUB_SHA": RUNNER_SHA, "ISCO_ENGINE_SHA": ENGINE_SHA}, clear=False), patches[0], patches[1], patches[2], patches[3], patch.object(
            resume_delivery, "finalize_short_quality", return_value={"delivery_allowed": True}
        ) as short_final:
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
        self.assertFalse(result["parent_media_rebuilt"])
        self.assertEqual(result["sibling_shorts_produced_after_parent_gold"], 0)
        self.assertEqual(result["delivery_kind"], "short")

    def test_long_plus_shorts_runs_only_sibling_continuation_after_parent_gold(self) -> None:
        root = self._root(fmt="film")
        request = self._request(kind="long", scope="long_plus_sibling_shorts", fmt="film")
        request_path, manifest_path, result_path = self._paths(root, request)
        before = _sha(root / "final.mp4")
        staged = [
            {"semantic_job": "one", "video": "short-1.mp4", "delivery_allowed": True},
            {"semantic_job": "two", "video": "short-2.mp4", "delivery_allowed": True},
        ]
        patches = self._common_patches(root)
        with patch.dict(os.environ, {"GITHUB_SHA": RUNNER_SHA, "ISCO_ENGINE_SHA": ENGINE_SHA}, clear=False), patches[0], patches[1], patches[2], patches[3], patch.object(
            resume_delivery, "_produce_approved_sibling_shorts", return_value=staged
        ) as siblings:
            result = resume_delivery.finalize_after_gold_resume(
                output_dir=root,
                request_path=request_path,
                resume_manifest_path=manifest_path,
                release_tag="video-225",
                runtime_root=root.parent / "runtime",
                result_output=result_path,
            )
        self.assertEqual(_sha(root / "final.mp4"), before)
        siblings.assert_called_once()
        self.assertEqual(result["sibling_shorts_produced_after_parent_gold"], 2)
        self.assertEqual(result["delivery_kind"], "long_plus_shorts")
        self.assertFalse(result["parent_media_rebuilt"])

    def test_long_only_never_invokes_sibling_production(self) -> None:
        root = self._root(fmt="film")
        request = self._request(kind="long", scope="long_only", fmt="film")
        request_path, manifest_path, result_path = self._paths(root, request)
        patches = self._common_patches(root)
        with patches[0], patches[1], patches[2], patches[3], patch.object(
            resume_delivery, "_produce_approved_sibling_shorts"
        ) as siblings:
            result = resume_delivery.finalize_after_gold_resume(
                output_dir=root,
                request_path=request_path,
                resume_manifest_path=manifest_path,
                release_tag="video-225",
                runtime_root=root.parent / "runtime",
                result_output=result_path,
            )
        siblings.assert_not_called()
        self.assertEqual(result["delivery_kind"], "long")


if __name__ == "__main__":
    unittest.main()

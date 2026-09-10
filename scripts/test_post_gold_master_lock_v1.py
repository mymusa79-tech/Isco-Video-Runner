from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import post_gold_master_lock_v1 as lock


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PostGoldMasterLockV1Tests(unittest.TestCase):
    def _root(self) -> Path:
        root = Path(tempfile.mkdtemp(prefix="master-lock-"))
        (root / "final.mp4").write_bytes(b"accepted-parent")
        (root / "final-master-qc.json").write_text('{"status":"pass"}', encoding="utf-8")
        (root / "gold-enforce-report.json").write_text(
            json.dumps(
                {
                    "phase": "4",
                    "mode": "enforce",
                    "gold": {"accepted": True},
                    "viewer_quality": {"verdict": "pass"},
                    "same_render": {"artifact_divergence": False},
                }
            ),
            encoding="utf-8",
        )
        return root

    def _acceptance(self, root: Path) -> dict:
        return {
            "status": "pass",
            "acceptance_contract": {
                "sources": {"final": {"sha256": _sha(root / "final.mp4")}}
            },
        }

    def test_lock_is_deterministic_and_idempotent(self) -> None:
        root = self._root()
        acceptance = self._acceptance(root)
        request = {
            "request_id": "req-1",
            "request_sha256": "abc",
            "kind": "long",
            "approval_scope": "long_plus_sibling_shorts",
            "approved_topic": "topic",
        }
        source = {
            "run_id": "123",
            "run_attempt": "1",
            "runner_sha": "a" * 40,
            "engine_sha": "b" * 40,
        }
        with patch.object(lock, "require_final_master_acceptance", return_value=acceptance):
            first = lock.write_master_lock(
                root,
                request=request,
                source=source,
                release_tag="video-1",
                expected_final_sha=_sha(root / "final.mp4"),
            )
            before = first.read_bytes()
            second = lock.write_master_lock(
                root,
                request=request,
                source=source,
                release_tag="video-1",
                expected_final_sha=_sha(root / "final.mp4"),
            )
        self.assertEqual(first, second)
        self.assertEqual(before, second.read_bytes())
        document = json.loads(first.read_text(encoding="utf-8"))
        self.assertEqual(document["state"], "locked_after_gold")
        self.assertFalse(document["mutation_allowed"])
        self.assertFalse(document["parent_media_rebuild_allowed"])
        self.assertEqual(document["final"]["sha256"], _sha(root / "final.mp4"))

    def test_parent_media_mutation_fails_closed(self) -> None:
        root = self._root()
        accepted_sha = _sha(root / "final.mp4")
        acceptance = self._acceptance(root)
        with patch.object(lock, "require_final_master_acceptance", return_value=acceptance):
            lock.write_master_lock(root, expected_final_sha=accepted_sha)
        (root / "final.mp4").write_bytes(b"mutated-parent")
        with patch.object(lock, "require_final_master_acceptance", return_value=acceptance):
            with self.assertRaisesRegex(RuntimeError, "final.mp4 changed"):
                lock.assert_master_lock(root)

    def test_gold_evidence_mutation_fails_closed(self) -> None:
        root = self._root()
        acceptance = self._acceptance(root)
        with patch.object(lock, "require_final_master_acceptance", return_value=acceptance):
            lock.write_master_lock(root)
        report = json.loads((root / "gold-enforce-report.json").read_text(encoding="utf-8"))
        report["viewer_quality"]["score_10"] = 9.9
        (root / "gold-enforce-report.json").write_text(json.dumps(report), encoding="utf-8")
        with patch.object(lock, "require_final_master_acceptance", return_value=acceptance):
            with self.assertRaisesRegex(RuntimeError, "Gold evidence changed"):
                lock.assert_master_lock(root)

    def test_nonaccepted_gold_cannot_be_locked(self) -> None:
        root = self._root()
        report = json.loads((root / "gold-enforce-report.json").read_text(encoding="utf-8"))
        report["gold"]["accepted"] = False
        (root / "gold-enforce-report.json").write_text(json.dumps(report), encoding="utf-8")
        with patch.object(lock, "require_final_master_acceptance", return_value=self._acceptance(root)):
            with self.assertRaisesRegex(RuntimeError, "accepted Gold"):
                lock.write_master_lock(root)


if __name__ == "__main__":
    unittest.main()

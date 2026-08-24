from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from scripts.release_transaction import publish_release_transaction


class ReleaseTransactionTests(unittest.TestCase):
    def _asset(self, root: Path, name: str, size: int) -> Path:
        path = root / name
        path.write_bytes(b"x" * size)
        return path

    def test_upload_failure_keeps_release_unpublished_and_attempts_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            asset = self._asset(root, "final.mp4", 10)
            journal = root / "journal.json"
            calls: list[list[str]] = []

            def run(args, **kwargs):
                calls.append(list(args))
                if args[:3] == ["gh", "release", "create"]:
                    return Mock(returncode=0, stdout="", stderr="")
                if args[:3] == ["gh", "release", "upload"]:
                    return Mock(returncode=1, stdout="", stderr="boom")
                if args[:3] == ["gh", "release", "view"]:
                    payload = {"tagName": "video-1", "isDraft": True, "assets": []}
                    return Mock(returncode=0, stdout=json.dumps(payload), stderr="")
                if args[:3] == ["gh", "release", "delete"]:
                    return Mock(returncode=0, stdout="", stderr="")
                raise AssertionError(args)

            with self.assertRaisesRegex(RuntimeError, "asset upload failed"):
                publish_release_transaction(
                    repository="o/r", tag="video-1", title="x", notes="x", assets=[asset], journal=journal, run=run
                )
            self.assertTrue(any(call[:3] == ["gh", "release", "delete"] for call in calls))
            state = json.loads(journal.read_text(encoding="utf-8"))["state"]
            self.assertEqual(state, "rolled_back_or_draft_retained")

    def test_remote_asset_mismatch_blocks_publication(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            asset = self._asset(root, "final.mp4", 10)
            journal = root / "journal.json"
            calls: list[list[str]] = []

            def run(args, **kwargs):
                calls.append(list(args))
                if args[:3] in (["gh", "release", "create"], ["gh", "release", "upload"], ["gh", "release", "delete"]):
                    return Mock(returncode=0, stdout="", stderr="")
                if args[:3] == ["gh", "release", "view"]:
                    payload = {"tagName": "video-1", "isDraft": True, "assets": [{"name": "final.mp4", "size": 9}]}
                    return Mock(returncode=0, stdout=json.dumps(payload), stderr="")
                raise AssertionError(args)

            with self.assertRaisesRegex(RuntimeError, "do not exactly match"):
                publish_release_transaction(
                    repository="o/r", tag="video-1", title="x", notes="x", assets=[asset], journal=journal, run=run
                )
            self.assertFalse(any(call[:3] == ["gh", "release", "edit"] for call in calls))

    def test_publish_is_last_mutation_after_exact_remote_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            asset = self._asset(root, "final.mp4", 10)
            journal = root / "journal.json"
            calls: list[list[str]] = []
            view_count = 0

            def run(args, **kwargs):
                nonlocal view_count
                calls.append(list(args))
                if args[:3] in (["gh", "release", "create"], ["gh", "release", "upload"], ["gh", "release", "edit"]):
                    return Mock(returncode=0, stdout="", stderr="")
                if args[:3] == ["gh", "release", "view"]:
                    view_count += 1
                    payload = {
                        "tagName": "video-1",
                        "isDraft": view_count == 1,
                        "assets": [{"name": "final.mp4", "size": 10}],
                    }
                    return Mock(returncode=0, stdout=json.dumps(payload), stderr="")
                raise AssertionError(args)

            publish_release_transaction(
                repository="o/r", tag="video-1", title="x", notes="x", assets=[asset], journal=journal, run=run
            )
            state = json.loads(journal.read_text(encoding="utf-8"))["state"]
            self.assertEqual(state, "complete")
            upload_i = next(i for i, call in enumerate(calls) if call[:3] == ["gh", "release", "upload"])
            edit_i = next(i for i, call in enumerate(calls) if call[:3] == ["gh", "release", "edit"])
            self.assertLess(upload_i, edit_i)

    def test_duplicate_asset_basenames_fail_before_github_side_effect(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a = root / "a" / "x.json"
            b = root / "b" / "x.json"
            a.parent.mkdir(); b.parent.mkdir()
            a.write_text("a"); b.write_text("b")
            run = Mock()
            with self.assertRaisesRegex(RuntimeError, "duplicate basenames"):
                publish_release_transaction(
                    repository="o/r", tag="video-1", title="x", notes="x", assets=[a, b], journal=root / "j.json", run=run
                )
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()

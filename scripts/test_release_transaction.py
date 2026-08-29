from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from scripts.release_transaction import (
    METADATA_TIMEOUT_SECONDS,
    UPLOAD_TIMEOUT_SECONDS,
    publish_release_transaction,
)


TARGET_SHA = "1" * 40


class ReleaseTransactionTests(unittest.TestCase):
    def _asset(self, root: Path, name: str, size: int) -> Path:
        path = root / name
        path.write_bytes(b"x" * size)
        return path

    def _digest(self, path: Path) -> str:
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()

    def _remote_asset(
        self,
        path: Path,
        *,
        size: int | None = None,
        digest: str | None = None,
        include_digest: bool = True,
    ) -> dict:
        item = {"name": path.name, "size": path.stat().st_size if size is None else size}
        if include_digest:
            item["digest"] = self._digest(path) if digest is None else digest
        return item

    def _payload(
        self,
        *,
        assets: list[dict],
        draft: bool,
        target: str = TARGET_SHA,
        tag: str = "video-1",
    ) -> dict:
        return {
            "tagName": tag,
            "targetCommitish": target,
            "isDraft": draft,
            "assets": assets,
        }

    @staticmethod
    def _not_found() -> Mock:
        return Mock(returncode=1, stdout="", stderr="release not found")

    def _publish(self, asset: Path, journal: Path, run) -> None:
        publish_release_transaction(
            repository="o/r",
            tag="video-1",
            target_sha=TARGET_SHA,
            title="x",
            notes="x",
            assets=[asset],
            journal=journal,
            run=run,
        )

    def test_upload_failure_cleans_draft_and_git_tag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            asset = self._asset(root, "final.mp4", 10)
            journal = root / "journal.json"
            calls: list[list[str]] = []
            view_count = 0

            def run(args, **kwargs):
                nonlocal view_count
                calls.append(list(args))
                if args[:3] == ["gh", "release", "view"]:
                    view_count += 1
                    if view_count == 1:
                        return self._not_found()
                    return Mock(
                        returncode=0,
                        stdout=json.dumps(self._payload(assets=[], draft=True)),
                        stderr="",
                    )
                if args[:3] == ["gh", "release", "create"]:
                    return Mock(returncode=0, stdout="", stderr="")
                if args[:3] == ["gh", "release", "upload"]:
                    return Mock(returncode=1, stdout="", stderr="boom")
                if args[:3] == ["gh", "release", "delete"]:
                    return Mock(returncode=0, stdout="", stderr="")
                raise AssertionError(args)

            with self.assertRaisesRegex(RuntimeError, "asset upload failed"):
                self._publish(asset, journal, run)
            delete = next(call for call in calls if call[:3] == ["gh", "release", "delete"])
            self.assertIn("--cleanup-tag", delete)
            state = json.loads(journal.read_text(encoding="utf-8"))["state"]
            self.assertEqual(state, "rolled_back_or_draft_retained")

    def test_remote_asset_mismatch_blocks_publication(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            asset = self._asset(root, "final.mp4", 10)
            journal = root / "journal.json"
            calls: list[list[str]] = []
            view_count = 0

            def run(args, **kwargs):
                nonlocal view_count
                calls.append(list(args))
                if args[:3] == ["gh", "release", "view"]:
                    view_count += 1
                    if view_count == 1:
                        return self._not_found()
                    payload = self._payload(assets=[self._remote_asset(asset, size=9)], draft=True)
                    return Mock(returncode=0, stdout=json.dumps(payload), stderr="")
                if args[:3] in (["gh", "release", "create"], ["gh", "release", "upload"], ["gh", "release", "delete"]):
                    return Mock(returncode=0, stdout="", stderr="")
                raise AssertionError(args)

            with self.assertRaisesRegex(RuntimeError, "do not exactly match"):
                self._publish(asset, journal, run)
            self.assertFalse(any(call[:3] == ["gh", "release", "edit"] for call in calls))

    def test_publish_is_last_mutation_after_exact_remote_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            asset = self._asset(root, "final.mp4", 10)
            journal = root / "journal.json"
            calls: list[tuple[list[str], dict]] = []
            view_count = 0

            def run(args, **kwargs):
                nonlocal view_count
                calls.append((list(args), dict(kwargs)))
                if args[:3] == ["gh", "release", "view"]:
                    view_count += 1
                    if view_count == 1:
                        return self._not_found()
                    payload = self._payload(
                        assets=[self._remote_asset(asset)], draft=view_count == 2
                    )
                    return Mock(returncode=0, stdout=json.dumps(payload), stderr="")
                if args[:3] in (["gh", "release", "create"], ["gh", "release", "upload"], ["gh", "release", "edit"]):
                    return Mock(returncode=0, stdout="", stderr="")
                raise AssertionError(args)

            self._publish(asset, journal, run)
            evidence = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(evidence["schema_version"], 2)
            self.assertEqual(evidence["state"], "complete")
            self.assertEqual(evidence["target_sha"], TARGET_SHA)
            self.assertEqual(evidence["asset_digests"], {asset.name: self._digest(asset)})

            command_calls = [item[0] for item in calls]
            upload_i = next(i for i, call in enumerate(command_calls) if call[:3] == ["gh", "release", "upload"])
            edit_i = next(i for i, call in enumerate(command_calls) if call[:3] == ["gh", "release", "edit"])
            self.assertLess(upload_i, edit_i)
            create_args, create_kwargs = next(item for item in calls if item[0][:3] == ["gh", "release", "create"])
            self.assertEqual(create_args[create_args.index("--target") + 1], TARGET_SHA)
            self.assertEqual(create_kwargs["timeout"], METADATA_TIMEOUT_SECONDS)
            upload_kwargs = next(item[1] for item in calls if item[0][:3] == ["gh", "release", "upload"])
            self.assertEqual(upload_kwargs["timeout"], UPLOAD_TIMEOUT_SECONDS)

    def test_existing_exact_published_release_reconciles_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            asset = self._asset(root, "final.mp4", 10)
            journal = root / "journal.json"
            calls: list[list[str]] = []

            def run(args, **kwargs):
                calls.append(list(args))
                if args[:3] == ["gh", "release", "view"]:
                    payload = self._payload(
                        assets=[self._remote_asset(asset)],
                        draft=False,
                    )
                    return Mock(returncode=0, stdout=json.dumps(payload), stderr="")
                raise AssertionError(f"published reconciliation must not mutate GitHub: {args}")

            self._publish(asset, journal, run)
            evidence = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(evidence["state"], "complete")
            self.assertEqual(evidence["detail"], "reconciled_existing_published")
            self.assertEqual(evidence["assets_verified"], 1)
            self.assertEqual(len(calls), 1)

    def test_existing_published_asset_mismatch_fails_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            asset = self._asset(root, "final.mp4", 10)
            journal = root / "journal.json"
            calls: list[list[str]] = []

            def run(args, **kwargs):
                calls.append(list(args))
                if args[:3] == ["gh", "release", "view"]:
                    payload = self._payload(
                        assets=[self._remote_asset(asset, digest="sha256:" + "0" * 64)],
                        draft=False,
                    )
                    return Mock(returncode=0, stdout=json.dumps(payload), stderr="")
                raise AssertionError(f"published conflict must not mutate GitHub: {args}")

            with self.assertRaisesRegex(RuntimeError, "do not exactly match"):
                self._publish(asset, journal, run)
            evidence = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(evidence["state"], "existing_published_conflict")
            self.assertEqual(len(calls), 1)

    def test_existing_published_wrong_target_fails_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            asset = self._asset(root, "final.mp4", 10)
            journal = root / "journal.json"
            calls: list[list[str]] = []

            def run(args, **kwargs):
                calls.append(list(args))
                if args[:3] == ["gh", "release", "view"]:
                    payload = self._payload(
                        assets=[self._remote_asset(asset)],
                        draft=False,
                        target="2" * 40,
                    )
                    return Mock(returncode=0, stdout=json.dumps(payload), stderr="")
                raise AssertionError(f"wrong-target conflict must not mutate GitHub: {args}")

            with self.assertRaisesRegex(RuntimeError, "reviewed Runner SHA"):
                self._publish(asset, journal, run)
            evidence = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(evidence["state"], "existing_release_conflict")
            self.assertEqual(len(calls), 1)

    def test_existing_same_target_draft_is_removed_with_tag_and_rebuilt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            asset = self._asset(root, "final.mp4", 10)
            journal = root / "journal.json"
            calls: list[list[str]] = []
            view_count = 0

            def run(args, **kwargs):
                nonlocal view_count
                calls.append(list(args))
                if args[:3] == ["gh", "release", "view"]:
                    view_count += 1
                    if view_count in {1, 2}:
                        payload = self._payload(assets=[], draft=True)
                        return Mock(returncode=0, stdout=json.dumps(payload), stderr="")
                    if view_count == 3:
                        return self._not_found()
                    payload = self._payload(
                        assets=[self._remote_asset(asset)],
                        draft=view_count == 4,
                    )
                    return Mock(returncode=0, stdout=json.dumps(payload), stderr="")
                if args[:3] in (
                    ["gh", "release", "delete"],
                    ["gh", "release", "create"],
                    ["gh", "release", "upload"],
                    ["gh", "release", "edit"],
                ):
                    return Mock(returncode=0, stdout="", stderr="")
                raise AssertionError(args)

            self._publish(asset, journal, run)
            delete = next(call for call in calls if call[:3] == ["gh", "release", "delete"])
            self.assertIn("--cleanup-tag", delete)
            create_i = next(i for i, call in enumerate(calls) if call[:3] == ["gh", "release", "create"])
            delete_i = next(i for i, call in enumerate(calls) if call[:3] == ["gh", "release", "delete"])
            self.assertLess(delete_i, create_i)
            evidence = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(evidence["state"], "complete")

    def test_existing_draft_target_change_blocks_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            asset = self._asset(root, "final.mp4", 10)
            journal = root / "journal.json"
            calls: list[list[str]] = []
            view_count = 0

            def run(args, **kwargs):
                nonlocal view_count
                calls.append(list(args))
                if args[:3] == ["gh", "release", "view"]:
                    view_count += 1
                    target = TARGET_SHA if view_count == 1 else "2" * 40
                    payload = self._payload(assets=[], draft=True, target=target)
                    return Mock(returncode=0, stdout=json.dumps(payload), stderr="")
                raise AssertionError(args)

            with self.assertRaisesRegex(RuntimeError, "reviewed Runner SHA"):
                self._publish(asset, journal, run)
            self.assertFalse(any(call[:3] == ["gh", "release", "delete"] for call in calls))
            evidence = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(evidence["state"], "existing_draft_cleanup_failed")

    def test_uncertain_reconciliation_probe_fails_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            asset = self._asset(root, "final.mp4", 10)
            journal = root / "journal.json"
            calls: list[list[str]] = []

            def run(args, **kwargs):
                calls.append(list(args))
                return Mock(returncode=1, stdout="", stderr="network unavailable")

            with self.assertRaisesRegex(RuntimeError, "could not prove"):
                self._publish(asset, journal, run)
            evidence = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(evidence["state"], "reconciliation_probe_failed")
            self.assertEqual(len(calls), 1)

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
                    repository="o/r",
                    tag="video-1",
                    target_sha=TARGET_SHA,
                    title="x",
                    notes="x",
                    assets=[a, b],
                    journal=root / "j.json",
                    run=run,
                )
            run.assert_not_called()

    def test_invalid_target_sha_fails_before_github_side_effect(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            asset = self._asset(root, "final.mp4", 10)
            run = Mock()
            with self.assertRaisesRegex(RuntimeError, "exact 40-character"):
                publish_release_transaction(
                    repository="o/r",
                    tag="video-1",
                    target_sha="main",
                    title="x",
                    notes="x",
                    assets=[asset],
                    journal=root / "j.json",
                    run=run,
                )
            run.assert_not_called()

    def test_missing_digest_fails_closed_before_publication(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            asset = self._asset(root, "final.mp4", 10)
            journal = root / "journal.json"
            calls: list[list[str]] = []
            view_count = 0

            def run(args, **kwargs):
                nonlocal view_count
                calls.append(list(args))
                if args[:3] == ["gh", "release", "view"]:
                    view_count += 1
                    if view_count == 1:
                        return self._not_found()
                    payload = self._payload(
                        assets=[self._remote_asset(asset, include_digest=False)], draft=True
                    )
                    return Mock(returncode=0, stdout=json.dumps(payload), stderr="")
                if args[:3] in (["gh", "release", "create"], ["gh", "release", "upload"], ["gh", "release", "delete"]):
                    return Mock(returncode=0, stdout="", stderr="")
                raise AssertionError(args)

            with self.assertRaisesRegex(RuntimeError, "digest is missing or malformed"):
                self._publish(asset, journal, run)
            self.assertFalse(any(call[:3] == ["gh", "release", "edit"] for call in calls))

    def test_wrong_digest_fails_closed_before_publication(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            asset = self._asset(root, "final.mp4", 10)
            journal = root / "journal.json"
            calls: list[list[str]] = []
            view_count = 0

            def run(args, **kwargs):
                nonlocal view_count
                calls.append(list(args))
                if args[:3] == ["gh", "release", "view"]:
                    view_count += 1
                    if view_count == 1:
                        return self._not_found()
                    payload = self._payload(
                        assets=[self._remote_asset(asset, digest="sha256:" + "0" * 64)],
                        draft=True,
                    )
                    return Mock(returncode=0, stdout=json.dumps(payload), stderr="")
                if args[:3] in (["gh", "release", "create"], ["gh", "release", "upload"], ["gh", "release", "delete"]):
                    return Mock(returncode=0, stdout="", stderr="")
                raise AssertionError(args)

            with self.assertRaisesRegex(RuntimeError, "do not exactly match"):
                self._publish(asset, journal, run)
            self.assertFalse(any(call[:3] == ["gh", "release", "edit"] for call in calls))

    def test_wrong_target_sha_fails_closed_before_publication(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            asset = self._asset(root, "final.mp4", 10)
            journal = root / "journal.json"
            calls: list[list[str]] = []
            view_count = 0

            def run(args, **kwargs):
                nonlocal view_count
                calls.append(list(args))
                if args[:3] == ["gh", "release", "view"]:
                    view_count += 1
                    if view_count == 1:
                        return self._not_found()
                    payload = self._payload(
                        assets=[self._remote_asset(asset)], draft=True, target="2" * 40
                    )
                    return Mock(returncode=0, stdout=json.dumps(payload), stderr="")
                if args[:3] in (["gh", "release", "create"], ["gh", "release", "upload"], ["gh", "release", "delete"]):
                    return Mock(returncode=0, stdout="", stderr="")
                raise AssertionError(args)

            with self.assertRaisesRegex(RuntimeError, "reviewed Runner SHA"):
                self._publish(asset, journal, run)
            self.assertFalse(any(call[:3] == ["gh", "release", "edit"] for call in calls))

    def test_duplicate_remote_names_fail_closed_before_publication(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            asset = self._asset(root, "final.mp4", 10)
            journal = root / "journal.json"
            calls: list[list[str]] = []
            view_count = 0

            def run(args, **kwargs):
                nonlocal view_count
                calls.append(list(args))
                if args[:3] == ["gh", "release", "view"]:
                    view_count += 1
                    if view_count == 1:
                        return self._not_found()
                    item = self._remote_asset(asset)
                    payload = self._payload(assets=[item, dict(item)], draft=True)
                    return Mock(returncode=0, stdout=json.dumps(payload), stderr="")
                if args[:3] in (["gh", "release", "create"], ["gh", "release", "upload"], ["gh", "release", "delete"]):
                    return Mock(returncode=0, stdout="", stderr="")
                raise AssertionError(args)

            with self.assertRaisesRegex(RuntimeError, "duplicate asset names"):
                self._publish(asset, journal, run)
            self.assertFalse(any(call[:3] == ["gh", "release", "edit"] for call in calls))

    def test_post_publish_digest_drift_is_reported_without_destructive_delete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            asset = self._asset(root, "final.mp4", 10)
            journal = root / "journal.json"
            calls: list[list[str]] = []
            view_count = 0

            def run(args, **kwargs):
                nonlocal view_count
                calls.append(list(args))
                if args[:3] == ["gh", "release", "view"]:
                    view_count += 1
                    if view_count == 1:
                        return self._not_found()
                    digest = self._digest(asset) if view_count == 2 else "sha256:" + "0" * 64
                    payload = self._payload(
                        assets=[self._remote_asset(asset, digest=digest)],
                        draft=view_count == 2,
                    )
                    return Mock(returncode=0, stdout=json.dumps(payload), stderr="")
                if args[:3] in (["gh", "release", "create"], ["gh", "release", "upload"], ["gh", "release", "edit"]):
                    return Mock(returncode=0, stdout="", stderr="")
                raise AssertionError(args)

            with self.assertRaisesRegex(RuntimeError, "drifted after publication"):
                self._publish(asset, journal, run)
            evidence = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(evidence["state"], "post_publish_verification_failed")
            self.assertFalse(any(call[:3] == ["gh", "release", "delete"] for call in calls))

    def test_create_timeout_is_bounded_and_journaled_for_later_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            asset = self._asset(root, "final.mp4", 10)
            journal = root / "journal.json"
            calls = 0

            def run(args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    return self._not_found()
                raise subprocess.TimeoutExpired(args, kwargs["timeout"])

            with self.assertRaisesRegex(RuntimeError, "creation timed out"):
                self._publish(asset, journal, run)
            evidence = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(evidence["state"], "create_failed")


if __name__ == "__main__":
    unittest.main()

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
    RECEIPT_NAME,
    RECEIPT_SCHEMA_VERSION,
    UPLOAD_TIMEOUT_SECONDS,
    publish_release_transaction,
)


TARGET_SHA = "1" * 40


class ReleaseTransactionTests(unittest.TestCase):
    def _asset(self, root: Path, name: str, size: int, byte: bytes = b"x") -> Path:
        path = root / name
        path.write_bytes(byte * size)
        return path

    def _digest(self, path: Path) -> str:
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()

    def _identity(self, path: Path) -> dict[str, object]:
        return {"size": path.stat().st_size, "digest": self._digest(path)}

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

    def _receipt_bytes(self, assets: dict[str, dict[str, object]]) -> bytes:
        payload = {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "tag": "video-1",
            "target_sha": TARGET_SHA,
            "assets": assets,
        }
        return (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")

    def _remote_receipt_asset(self, receipt_bytes: bytes) -> dict:
        return {
            "name": RECEIPT_NAME,
            "size": len(receipt_bytes),
            "digest": "sha256:" + hashlib.sha256(receipt_bytes).hexdigest(),
        }

    def _write_downloaded_receipt(self, args: list[str], receipt_bytes: bytes) -> None:
        dest = Path(args[args.index("--dir") + 1]) / RECEIPT_NAME
        dest.write_bytes(receipt_bytes)

    def _publish_assets(self, assets: list[Path], journal: Path, run) -> None:
        publish_release_transaction(
            repository="o/r",
            tag="video-1",
            target_sha=TARGET_SHA,
            title="x",
            notes="x",
            assets=assets,
            journal=journal,
            run=run,
        )

    def _publish(self, asset: Path, journal: Path, run) -> None:
        self._publish_assets([asset], journal, run)

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
                    return Mock(returncode=0, stdout=json.dumps(self._payload(assets=[], draft=True)), stderr="")
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

    def test_normal_publish_includes_receipt_and_publishes_only_after_exact_verification(self) -> None:
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
                    receipt = root / RECEIPT_NAME
                    payload = self._payload(
                        assets=[self._remote_asset(asset), self._remote_asset(receipt)],
                        draft=view_count == 2,
                    )
                    return Mock(returncode=0, stdout=json.dumps(payload), stderr="")
                if args[:3] in (["gh", "release", "create"], ["gh", "release", "upload"], ["gh", "release", "edit"]):
                    return Mock(returncode=0, stdout="", stderr="")
                raise AssertionError(args)

            self._publish(asset, journal, run)
            receipt = root / RECEIPT_NAME
            self.assertTrue(receipt.is_file())
            receipt_doc = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertEqual(receipt_doc["target_sha"], TARGET_SHA)
            self.assertEqual(receipt_doc["assets"], {asset.name: self._identity(asset)})

            evidence = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(evidence["schema_version"], 2)
            self.assertEqual(evidence["state"], "complete")
            self.assertEqual(evidence["assets_expected"], 2)
            self.assertEqual(evidence["assets_verified"], 2)
            self.assertEqual(evidence["target_sha"], TARGET_SHA)
            self.assertIn(RECEIPT_NAME, evidence["asset_digests"])

            command_calls = [item[0] for item in calls]
            upload_call = next(call for call in command_calls if call[:3] == ["gh", "release", "upload"])
            self.assertTrue(any(Path(value).name == RECEIPT_NAME for value in upload_call))
            upload_i = command_calls.index(upload_call)
            edit_i = next(i for i, call in enumerate(command_calls) if call[:3] == ["gh", "release", "edit"])
            self.assertLess(upload_i, edit_i)
            create_args, create_kwargs = next(item for item in calls if item[0][:3] == ["gh", "release", "create"])
            self.assertEqual(create_args[create_args.index("--target") + 1], TARGET_SHA)
            self.assertEqual(create_kwargs["timeout"], METADATA_TIMEOUT_SECONDS)
            upload_kwargs = next(item[1] for item in calls if item[0][:3] == ["gh", "release", "upload"])
            self.assertEqual(upload_kwargs["timeout"], UPLOAD_TIMEOUT_SECONDS)

    def test_existing_published_receipt_reconciles_when_video_matches_even_if_telemetry_changed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            final = self._asset(root, "final.mp4", 10, b"v")
            telemetry = root / "planning-telemetry.json"
            telemetry.write_text('{"attempt":2}', encoding="utf-8")
            prior_telemetry = root / "prior-telemetry.json"
            prior_telemetry.write_text('{"attempt":1}', encoding="utf-8")
            prior_assets = {
                final.name: self._identity(final),
                telemetry.name: self._identity(prior_telemetry),
            }
            receipt_bytes = self._receipt_bytes(prior_assets)
            remote_assets = [
                self._remote_asset(final),
                {"name": telemetry.name, **self._identity(prior_telemetry)},
                self._remote_receipt_asset(receipt_bytes),
            ]
            journal = root / "journal.json"
            calls: list[list[str]] = []

            def run(args, **kwargs):
                calls.append(list(args))
                if args[:3] == ["gh", "release", "view"]:
                    return Mock(returncode=0, stdout=json.dumps(self._payload(assets=remote_assets, draft=False)), stderr="")
                if args[:3] == ["gh", "release", "download"]:
                    self._write_downloaded_receipt(list(args), receipt_bytes)
                    return Mock(returncode=0, stdout="", stderr="")
                raise AssertionError(f"published reconciliation must not mutate GitHub: {args}")

            self._publish_assets([final, telemetry], journal, run)
            evidence = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(evidence["state"], "complete")
            self.assertEqual(evidence["detail"], "reconciled_existing_published_receipt")
            self.assertFalse(any(call[:3] in (["gh", "release", "create"], ["gh", "release", "upload"], ["gh", "release", "edit"], ["gh", "release", "delete"]) for call in calls))

    def test_existing_published_receipt_blocks_when_current_video_differs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = self._asset(root, "final.mp4", 10, b"n")
            prior = root / "prior-final.mp4"
            prior.write_bytes(b"p" * 10)
            prior_assets = {current.name: self._identity(prior)}
            receipt_bytes = self._receipt_bytes(prior_assets)
            remote_assets = [
                {"name": current.name, **self._identity(prior)},
                self._remote_receipt_asset(receipt_bytes),
            ]
            journal = root / "journal.json"
            calls: list[list[str]] = []

            def run(args, **kwargs):
                calls.append(list(args))
                if args[:3] == ["gh", "release", "view"]:
                    return Mock(returncode=0, stdout=json.dumps(self._payload(assets=remote_assets, draft=False)), stderr="")
                if args[:3] == ["gh", "release", "download"]:
                    self._write_downloaded_receipt(list(args), receipt_bytes)
                    return Mock(returncode=0, stdout="", stderr="")
                raise AssertionError(args)

            with self.assertRaisesRegex(RuntimeError, "current reviewed video bytes"):
                self._publish(current, journal, run)
            evidence = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(evidence["state"], "existing_published_conflict")
            self.assertFalse(any(call[:3] in (["gh", "release", "edit"], ["gh", "release", "delete"]) for call in calls))

    def test_existing_published_remote_asset_drift_from_receipt_blocks_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            final = self._asset(root, "final.mp4", 10)
            receipt_bytes = self._receipt_bytes({final.name: self._identity(final)})
            remote_assets = [
                self._remote_asset(final, digest="sha256:" + "0" * 64),
                self._remote_receipt_asset(receipt_bytes),
            ]
            journal = root / "journal.json"
            calls: list[list[str]] = []

            def run(args, **kwargs):
                calls.append(list(args))
                if args[:3] == ["gh", "release", "view"]:
                    return Mock(returncode=0, stdout=json.dumps(self._payload(assets=remote_assets, draft=False)), stderr="")
                if args[:3] == ["gh", "release", "download"]:
                    self._write_downloaded_receipt(list(args), receipt_bytes)
                    return Mock(returncode=0, stdout="", stderr="")
                raise AssertionError(args)

            with self.assertRaisesRegex(RuntimeError, "drifted from their durable reconciliation receipt"):
                self._publish(final, journal, run)
            self.assertFalse(any(call[:3] in (["gh", "release", "edit"], ["gh", "release", "delete"]) for call in calls))

    def test_existing_published_missing_receipt_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            final = self._asset(root, "final.mp4", 10)
            journal = root / "journal.json"
            calls: list[list[str]] = []

            def run(args, **kwargs):
                calls.append(list(args))
                if args[:3] == ["gh", "release", "view"]:
                    return Mock(returncode=0, stdout=json.dumps(self._payload(assets=[self._remote_asset(final)], draft=False)), stderr="")
                raise AssertionError(args)

            with self.assertRaisesRegex(RuntimeError, "missing durable reconciliation receipt"):
                self._publish(final, journal, run)
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
                    payload = self._payload(assets=[], draft=False, target="2" * 40)
                    return Mock(returncode=0, stdout=json.dumps(payload), stderr="")
                raise AssertionError(args)

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
                        return Mock(returncode=0, stdout=json.dumps(self._payload(assets=[], draft=True)), stderr="")
                    if view_count == 3:
                        return self._not_found()
                    receipt = root / RECEIPT_NAME
                    payload = self._payload(
                        assets=[self._remote_asset(asset), self._remote_asset(receipt)],
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
                    return Mock(returncode=0, stdout=json.dumps(self._payload(assets=[], draft=True, target=target)), stderr="")
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

    def test_prepublication_remote_mismatch_blocks_publication_and_rolls_back(self) -> None:
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
                    receipt = root / RECEIPT_NAME
                    if view_count == 2:
                        payload = self._payload(
                            assets=[self._remote_asset(asset, size=9), self._remote_asset(receipt)],
                            draft=True,
                        )
                    else:
                        payload = self._payload(assets=[], draft=True)
                    return Mock(returncode=0, stdout=json.dumps(payload), stderr="")
                if args[:3] in (["gh", "release", "create"], ["gh", "release", "upload"], ["gh", "release", "delete"]):
                    return Mock(returncode=0, stdout="", stderr="")
                raise AssertionError(args)

            with self.assertRaisesRegex(RuntimeError, "do not exactly match"):
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
                    receipt = root / RECEIPT_NAME
                    digest = self._digest(asset) if view_count == 2 else "sha256:" + "0" * 64
                    payload = self._payload(
                        assets=[self._remote_asset(asset, digest=digest), self._remote_asset(receipt)],
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

    def test_reserved_receipt_basename_fails_before_github_side_effect(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipt = root / RECEIPT_NAME
            receipt.write_text("{}", encoding="utf-8")
            run = Mock()
            with self.assertRaisesRegex(RuntimeError, "reserved durable receipt"):
                self._publish(receipt, root / "j.json", run)
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

    def test_create_timeout_is_bounded_and_journaled_for_later_draft_reconciliation(self) -> None:
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

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from scripts.release_transaction import RECEIPT_NAME, RECEIPT_SCHEMA_VERSION, publish_release_transaction


TARGET_SHA = "1" * 40


class ReleaseReconciliationJournalTests(unittest.TestCase):
    @staticmethod
    def _digest_bytes(data: bytes) -> str:
        return "sha256:" + hashlib.sha256(data).hexdigest()

    @classmethod
    def _identity_bytes(cls, data: bytes) -> dict[str, object]:
        return {"size": len(data), "digest": cls._digest_bytes(data)}

    @classmethod
    def _receipt_bytes(cls, assets: dict[str, dict[str, object]]) -> bytes:
        payload = {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "tag": "video-1",
            "target_sha": TARGET_SHA,
            "assets": assets,
        }
        return (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")

    def test_success_journal_records_remote_release_identity_not_retry_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            final_bytes = b"video" * 2048
            remote_telemetry = b'{"attempt":1}'
            retry_telemetry = b'{"attempt":2}'
            final = root / "final.mp4"
            telemetry = root / "planning-telemetry.json"
            final.write_bytes(final_bytes)
            telemetry.write_bytes(retry_telemetry)

            receipt_assets = {
                "final.mp4": self._identity_bytes(final_bytes),
                "planning-telemetry.json": self._identity_bytes(remote_telemetry),
            }
            receipt_bytes = self._receipt_bytes(receipt_assets)
            remote_identities = {
                **receipt_assets,
                RECEIPT_NAME: self._identity_bytes(receipt_bytes),
            }
            remote_payload = {
                "tagName": "video-1",
                "targetCommitish": TARGET_SHA,
                "isDraft": False,
                "assets": [
                    {"name": name, **identity}
                    for name, identity in remote_identities.items()
                ],
            }
            calls: list[list[str]] = []

            def run(args, **kwargs):
                calls.append(list(args))
                if args[:3] == ["gh", "release", "view"]:
                    return Mock(returncode=0, stdout=json.dumps(remote_payload), stderr="")
                if args[:3] == ["gh", "release", "download"]:
                    dest = Path(args[args.index("--dir") + 1]) / RECEIPT_NAME
                    dest.write_bytes(receipt_bytes)
                    return Mock(returncode=0, stdout="", stderr="")
                raise AssertionError(args)

            journal = root / "release-transaction.json"
            publish_release_transaction(
                repository="o/r",
                tag="video-1",
                target_sha=TARGET_SHA,
                title="x",
                notes="x",
                assets=[final, telemetry],
                journal=journal,
                run=run,
            )

            evidence = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(evidence["state"], "complete")
            self.assertEqual(evidence["detail"], "reconciled_existing_published_receipt")
            self.assertEqual(evidence["assets_expected"], len(remote_identities))
            self.assertEqual(evidence["assets_verified"], len(remote_identities))
            self.assertEqual(
                evidence["asset_digests"],
                {name: str(identity["digest"]) for name, identity in remote_identities.items()},
            )
            self.assertNotEqual(
                evidence["asset_digests"]["planning-telemetry.json"],
                self._digest_bytes(retry_telemetry),
            )
            self.assertFalse(
                any(call[:3] in (["gh", "release", "create"], ["gh", "release", "upload"], ["gh", "release", "edit"], ["gh", "release", "delete"]) for call in calls)
            )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts.delivery_acceptance_v2 import CONTRACT_ID, seal_delivery_acceptance


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _identity(path: Path) -> dict[str, object]:
    return {"size": path.stat().st_size, "digest": _digest(path)}


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


class DeliveryAcceptanceV2Tests(unittest.TestCase):
    def _fixture(self, root: Path) -> dict[str, Path | str]:
        final = root / "final.mp4"
        final.write_bytes(b"certified-final-video" * 300)
        final_hash = hashlib.sha256(final.read_bytes()).hexdigest()

        qc = root / "final-master-qc.json"
        _write(qc, {"status": "pass", "acceptance_contract": {"contract_id": "final.master.acceptance.v2"}})
        qc_identity = _identity(qc)

        delivery = root / "delivery-manifest.json"
        _write(
            delivery,
            {
                "schema_version": 2,
                "delivery_kind": "long",
                "release_state": "staged",
                "release_tag": None,
                "delivery_url": None,
                "release_candidate_tag": "video-777",
                "release_candidate_url": "https://github.com/mymusa79-tech/Isco-Video-Runner/releases/tag/video-777",
                "primary_video": "final.mp4",
                "primary_video_sha256": final_hash,
                "final_master_qc": {
                    "file": "final-master-qc.json",
                    "file_size": qc_identity["size"],
                    "file_sha256": str(qc_identity["digest"])[7:],
                    "evidence": {"status": "pass", "acceptance_contract": {"contract_id": "final.master.acceptance.v2"}},
                },
                "youtube_publish_mode": "manual_in_youtube_studio",
                "publication_performed": False,
                "partial_delivery_allowed": False,
            },
        )

        assets = {
            delivery.name: _identity(delivery),
            final.name: _identity(final),
            qc.name: _identity(qc),
        }
        release_receipt = root / "release-receipt.json"
        _write(
            release_receipt,
            {
                "schema_version": 1,
                "tag": "video-777",
                "target_sha": "a" * 40,
                "assets": assets,
            },
        )
        journal = root / "release-transaction.json"
        digests = {name: str(identity["digest"]) for name, identity in assets.items()}
        digests[release_receipt.name] = _digest(release_receipt)
        _write(
            journal,
            {
                "schema_version": 2,
                "state": "complete",
                "tag": "video-777",
                "target_sha": "a" * 40,
                "assets_expected": len(assets) + 1,
                "assets_verified": len(assets) + 1,
                "asset_digests": digests,
                "detail": "published_verified",
            },
        )
        return {
            "final": final,
            "qc": qc,
            "delivery": delivery,
            "release_receipt": release_receipt,
            "journal": journal,
            "output": root / "delivery-terminal-receipt.json",
        }

    def _seal(self, fixture: dict[str, Path | str]) -> dict:
        return seal_delivery_acceptance(
            delivery_manifest=Path(fixture["delivery"]),
            release_receipt=Path(fixture["release_receipt"]),
            release_journal=Path(fixture["journal"]),
            repository="mymusa79-tech/Isco-Video-Runner",
            release_tag="video-777",
            target_sha="a" * 40,
            output=Path(fixture["output"]),
        )

    def test_completed_release_receipt_is_the_only_terminal_released_authority(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fixture = self._fixture(Path(td))
            receipt = self._seal(fixture)
            self.assertEqual(receipt["contract_id"], CONTRACT_ID)
            self.assertEqual(receipt["decision"], "released")
            self.assertEqual(receipt["release_state"], "released")
            self.assertEqual(receipt["release_tag"], "video-777")
            self.assertTrue(receipt["delivery_url"].endswith("/releases/tag/video-777"))
            self.assertFalse(receipt["publication_performed"])
            self.assertEqual(receipt["youtube_publish_mode"], "manual_in_youtube_studio")
            self.assertEqual(receipt["delivery_manifest"]["digest"], _digest(Path(fixture["delivery"])))
            self.assertEqual(receipt["final_master_qc"]["digest"], _digest(Path(fixture["qc"])))
            self.assertTrue(Path(fixture["output"]).is_file())

    def test_preclaimed_released_manifest_is_rejected_even_with_valid_remote_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fixture = self._fixture(Path(td))
            path = Path(fixture["delivery"])
            value = json.loads(path.read_text(encoding="utf-8"))
            value["release_state"] = "released"
            _write(path, value)
            with self.assertRaisesRegex(RuntimeError, "must remain staged"):
                self._seal(fixture)

    def test_release_receipt_must_bind_exact_staged_manifest_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fixture = self._fixture(Path(td))
            path = Path(fixture["delivery"])
            value = json.loads(path.read_text(encoding="utf-8"))
            value["topic"] = "changed-after-review"
            _write(path, value)
            with self.assertRaisesRegex(RuntimeError, "exact staged Delivery manifest"):
                self._seal(fixture)

    def test_release_must_bind_exact_staged_final_master_qc_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fixture = self._fixture(Path(td))
            receipt_path = Path(fixture["release_receipt"])
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["assets"]["final-master-qc.json"] = {
                "size": receipt["assets"]["final-master-qc.json"]["size"],
                "digest": "sha256:" + "0" * 64,
            }
            _write(receipt_path, receipt)
            journal_path = Path(fixture["journal"])
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            journal["asset_digests"]["release-receipt.json"] = _digest(receipt_path)
            journal["asset_digests"]["final-master-qc.json"] = "sha256:" + "0" * 64
            _write(journal_path, journal)
            with self.assertRaisesRegex(RuntimeError, "exact staged QC evidence"):
                self._seal(fixture)

    def test_incomplete_release_journal_cannot_be_promoted_to_released(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fixture = self._fixture(Path(td))
            journal = Path(fixture["journal"])
            value = json.loads(journal.read_text(encoding="utf-8"))
            value["state"] = "uploaded_verified"
            _write(journal, value)
            with self.assertRaisesRegex(RuntimeError, "cannot become released"):
                self._seal(fixture)

    def test_journal_must_bind_exact_durable_release_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fixture = self._fixture(Path(td))
            journal = Path(fixture["journal"])
            value = json.loads(journal.read_text(encoding="utf-8"))
            value["asset_digests"]["release-receipt.json"] = "sha256:" + "0" * 64
            _write(journal, value)
            with self.assertRaisesRegex(RuntimeError, "exact durable Release receipt"):
                self._seal(fixture)


if __name__ == "__main__":
    unittest.main()

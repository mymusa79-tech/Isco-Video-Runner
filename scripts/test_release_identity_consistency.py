from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.unified_delivery import finalize_release_manifest


class ReleaseIdentityConsistencyTests(unittest.TestCase):
    def _staged(self, production_tag: str | None) -> Path:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        production = {"format": "film", "release_tag": production_tag}
        delivery = {
            "schema_version": 1,
            "release_state": "staged",
            "release_tag": None,
            "publication_performed": False,
        }
        (root / "production-manifest.json").write_text(
            json.dumps(production), encoding="utf-8"
        )
        path = root / "delivery-manifest.json"
        path.write_text(json.dumps(delivery), encoding="utf-8")
        return path

    def test_finalize_accepts_matching_production_release_identity(self) -> None:
        path = self._staged("telegram-req-abc123")
        finalize_release_manifest(
            path,
            repository="mymusa79-tech/Isco-Video-Runner",
            release_tag="telegram-req-abc123",
        )
        manifest = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["release_tag"], "telegram-req-abc123")
        self.assertEqual(manifest["release_state"], "released")

    def test_finalize_rejects_cross_manifest_release_identity_drift(self) -> None:
        path = self._staged("telegram-req-abc123")
        with self.assertRaisesRegex(RuntimeError, "Release identity mismatch"):
            finalize_release_manifest(
                path,
                repository="mymusa79-tech/Isco-Video-Runner",
                release_tag="video-999",
            )

    def test_finalize_keeps_legacy_unbound_fixture_compatible(self) -> None:
        path = self._staged(None)
        finalize_release_manifest(
            path,
            repository="mymusa79-tech/Isco-Video-Runner",
            release_tag="video-42",
        )
        manifest = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["release_tag"], "video-42")


if __name__ == "__main__":
    unittest.main()

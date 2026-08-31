from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import scripts.run_v3_voice as run_v3_voice


class ProductionManifestReleaseIdentityTests(unittest.TestCase):
    def test_manual_v4_keeps_video_run_number_tag(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "final.mp4").write_bytes(b"manual-final")
            with patch.dict(os.environ, {"GITHUB_RUN_NUMBER": "77"}, clear=True):
                manifest = run_v3_voice._write_production_manifest(
                    root, production_id="v4:manual:1", fmt="film"
                )
            self.assertEqual(manifest["release_tag"], "video-77")

    def test_telegram_release_override_is_the_manifest_authority(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "final.mp4").write_bytes(b"telegram-final")
            env = {
                "GITHUB_RUN_NUMBER": "88",
                "ISCO_RELEASE_TAG_OVERRIDE": "telegram-req-44fc8e8b55bb-acde1234",
            }
            with patch.dict(os.environ, env, clear=True):
                manifest = run_v3_voice._write_production_manifest(
                    root, production_id="v4:telegram:1", fmt="film"
                )
            self.assertEqual(
                manifest["release_tag"], "telegram-req-44fc8e8b55bb-acde1234"
            )
            self.assertNotEqual(manifest["release_tag"], "video-88")

    def test_manifest_hashes_exact_final_bytes(self) -> None:
        payload = b"non-empty-final-media-bytes"
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "final.mp4").write_bytes(payload)
            with patch.dict(os.environ, {"GITHUB_RUN_NUMBER": "99"}, clear=True):
                manifest = run_v3_voice._write_production_manifest(
                    root, production_id="v4:hash:1", fmt="film"
                )
            self.assertEqual(
                manifest["final_sha256"], hashlib.sha256(payload).hexdigest()
            )


if __name__ == "__main__":
    unittest.main()

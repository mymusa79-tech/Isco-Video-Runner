from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts.clean_v2_resume_cache import prepare_resume_cache


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_checkpoint(root: Path, payload: bytes = b"approved-script") -> None:
    root.mkdir(parents=True, exist_ok=True)
    artifact = root / "script.json"
    artifact.write_bytes(payload)
    (root / "resume-checkpoint.json").write_text(
        json.dumps(
            {
                "schema_version": 6,
                "completed_stage": "script",
                "artifacts": {"script.json": _sha256(artifact)},
            }
        )
        + "\n",
        encoding="utf-8",
    )


class CleanV2ResumeCacheTests(unittest.TestCase):
    def test_prepare_copies_only_hash_verified_checkpoint_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "output"
            target = root / "cache"
            _write_checkpoint(output)
            (output / "untrusted.txt").write_text("do not copy", encoding="utf-8")

            self.assertTrue(prepare_resume_cache(output, target))
            self.assertTrue((target / "resume-checkpoint.json").is_file())
            self.assertEqual((target / "script.json").read_bytes(), b"approved-script")
            self.assertFalse((target / "untrusted.txt").exists())

    def test_tampered_output_does_not_replace_last_verified_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trusted = root / "trusted"
            target = root / "cache"
            _write_checkpoint(trusted)
            self.assertTrue(prepare_resume_cache(trusted, target))

            tampered = root / "tampered"
            _write_checkpoint(tampered, b"new-script")
            (tampered / "script.json").write_bytes(b"changed-after-checkpoint")

            self.assertTrue(prepare_resume_cache(tampered, target))
            self.assertEqual((target / "script.json").read_bytes(), b"approved-script")

    def test_invalid_checkpoint_without_prior_cache_is_not_saved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "output"
            target = root / "cache"
            _write_checkpoint(output)
            (output / "script.json").unlink()

            self.assertFalse(prepare_resume_cache(output, target))
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()

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


class PostRenderMutationTests(unittest.TestCase):
    def test_failed_post_render_layer_keeps_voice_checkpoint_savable(self) -> None:
        from clean_v2 import pipeline

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "output"
            output.mkdir()
            (output / "cta-plan.json").write_text('{"mode":"like"}', encoding="utf-8")
            (output / "rights-manifest.json").write_text('{"assets":[1]}', encoding="utf-8")
            artifacts = {
                name: _sha256(output / name)
                for name in ("cta-plan.json", "rights-manifest.json")
            }
            (output / "resume-checkpoint.json").write_text(
                json.dumps({"schema_version": 6, "completed_stage": "visuals", "artifacts": artifacts}),
                encoding="utf-8",
            )
            snapshot = pipeline._snapshot_checkpoint_mutable_files(output)
            # A post-render layer mutates both files, then crashes.
            (output / "cta-plan.json").write_text('{"mode":"like","visual":1}', encoding="utf-8")
            (output / "rights-manifest.json").write_text('{"assets":[1],"x":2}', encoding="utf-8")
            self.assertFalse(prepare_resume_cache(output, root / "cache-before"))
            pipeline._restore_checkpoint_mutable_files(output, snapshot)
            self.assertTrue(prepare_resume_cache(output, root / "cache-after"))


if __name__ == "__main__":
    unittest.main()

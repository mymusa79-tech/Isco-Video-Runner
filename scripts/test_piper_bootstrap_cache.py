from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from scripts.piper_bootstrap_cache import (
    PiperBootstrapCacheError,
    validate_voice_directory,
    validate_wheel_directory,
)


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_WORKFLOW = ROOT / ".github" / "workflows" / "produce-resilient-v4.yml"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _voice_manifest(path: Path, files: dict[str, bytes]) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "voice": "ar_JO-kareem-medium",
                "source": "test",
                "source_ref": "immutable-test-fixture",
                "files": {
                    name: {"sha256": _sha(data), "size_bytes": len(data)}
                    for name, data in files.items()
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


class PiperBootstrapCacheTests(unittest.TestCase):
    def test_accepts_exact_regular_wheel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = b"verified-piper-wheel"
            wheel = root / "piper_tts-1.4.2-py3-none-any.whl"
            wheel.write_bytes(data)
            result = validate_wheel_directory(root, _sha(data))
            self.assertEqual(result["sha256"], _sha(data))
            self.assertEqual(Path(result["wheel"]).name, wheel.name)

    def test_rejects_wheel_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "piper_tts-1.4.2-py3-none-any.whl").write_bytes(b"poisoned")
            with self.assertRaisesRegex(PiperBootstrapCacheError, "sha256 mismatch"):
                validate_wheel_directory(root, _sha(b"expected"))

    def test_rejects_wheel_cache_with_extra_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = b"wheel"
            (root / "piper_tts-1.4.2-py3-none-any.whl").write_bytes(data)
            (root / "extra.txt").write_text("not-authoritative", encoding="utf-8")
            with self.assertRaisesRegex(PiperBootstrapCacheError, "exactly one entry"):
                validate_wheel_directory(root, _sha(data))

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink unavailable")
    def test_rejects_symlinked_wheel_even_if_target_hash_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root.parent / (root.name + "-outside.whl")
            data = b"matching-but-symlinked"
            outside.write_bytes(data)
            try:
                os.symlink(outside, root / "piper_tts-1.4.2-py3-none-any.whl")
                with self.assertRaisesRegex(PiperBootstrapCacheError, "not a regular file"):
                    validate_wheel_directory(root, _sha(data))
            finally:
                outside.unlink(missing_ok=True)

    def test_accepts_exact_voice_manifest_shape_and_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            voice = base / "voice"
            voice.mkdir()
            files = {
                "ar_JO-kareem-medium.onnx": b"model-bytes",
                "ar_JO-kareem-medium.onnx.json": b"config-bytes",
            }
            for name, data in files.items():
                (voice / name).write_bytes(data)
            manifest = base / "manifest.json"
            _voice_manifest(manifest, files)
            result = validate_voice_directory(voice, manifest)
            self.assertEqual(set(result), set(files))

    def test_rejects_voice_cache_extra_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            voice = base / "voice"
            voice.mkdir()
            files = {
                "ar_JO-kareem-medium.onnx": b"model",
                "ar_JO-kareem-medium.onnx.json": b"config",
            }
            for name, data in files.items():
                (voice / name).write_bytes(data)
            (voice / "unexpected.bin").write_bytes(b"x")
            manifest = base / "manifest.json"
            _voice_manifest(manifest, files)
            with self.assertRaisesRegex(PiperBootstrapCacheError, "shape mismatch"):
                validate_voice_directory(voice, manifest)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink unavailable")
    def test_rejects_symlinked_voice_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            voice = base / "voice"
            voice.mkdir()
            model = b"model"
            config = b"config"
            outside = base / "outside.onnx"
            outside.write_bytes(model)
            os.symlink(outside, voice / "ar_JO-kareem-medium.onnx")
            (voice / "ar_JO-kareem-medium.onnx.json").write_bytes(config)
            manifest = base / "manifest.json"
            _voice_manifest(
                manifest,
                {
                    "ar_JO-kareem-medium.onnx": model,
                    "ar_JO-kareem-medium.onnx.json": config,
                },
            )
            with self.assertRaisesRegex(PiperBootstrapCacheError, "not a regular file"):
                validate_voice_directory(voice, manifest)

    def test_production_cache_is_piper_only_and_revalidated(self) -> None:
        text = PRODUCTION_WORKFLOW.read_text(encoding="utf-8")
        for needle in (
            "Restore untrusted Piper wheel cache",
            "Restore untrusted Piper voice cache",
            "scripts/piper_bootstrap_cache.py wheel",
            "scripts/piper_bootstrap_cache.py voice",
            "piper_wheel_save=true",
            "piper_voice_save=true",
            "Save verified Piper wheel cache",
            "Save verified Piper voice cache",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, text)
        self.assertNotIn("cache: pip", text)
        self.assertNotIn("/var/cache/apt", text)

    def test_cache_keys_bind_immutable_artifact_identity_and_allow_recovery(self) -> None:
        text = PRODUCTION_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn(
            "piper-wheel-v2-${{ runner.os }}-py312-b17184a664bd9431ce95c138f4bfb3025e1280cf26075a703dbfdcab989b8ee3-${{ github.run_id }}",
            text,
        )
        self.assertIn(
            "piper-wheel-v2-${{ runner.os }}-py312-b17184a664bd9431ce95c138f4bfb3025e1280cf26075a703dbfdcab989b8ee3-",
            text,
        )
        self.assertIn("hashFiles('engine/security/piper_voice_hashes.json')", text)
        self.assertIn("-${{ github.run_id }}", text)


if __name__ == "__main__":
    unittest.main()

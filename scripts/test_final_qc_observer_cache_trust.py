from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.final_qc_observer_cache_trust import sanitize_final_observer_cache_before_runtime


class FinalQcObserverCacheTrustTests(unittest.TestCase):
    def test_symlinked_namespace_is_removed_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "cache"
            target = root / "outside"
            cache.mkdir()
            target.mkdir()
            marker = target / "keep.txt"
            marker.write_text("keep", encoding="utf-8")
            (cache / "observers").symlink_to(target, target_is_directory=True)

            with patch.dict(os.environ, {"ISCO_TTS_CACHE_PATH": str(cache)}, clear=False):
                self.assertFalse(sanitize_final_observer_cache_before_runtime())

            self.assertFalse((cache / "observers").exists())
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_symlinked_shared_root_becomes_clean_real_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "outside"
            target.mkdir()
            marker = target / "keep.txt"
            marker.write_text("keep", encoding="utf-8")
            cache = root / "cache"
            cache.symlink_to(target, target_is_directory=True)

            with patch.dict(os.environ, {"ISCO_TTS_CACHE_PATH": str(cache)}, clear=False):
                self.assertFalse(sanitize_final_observer_cache_before_runtime())

            self.assertTrue(cache.is_dir())
            self.assertFalse(cache.is_symlink())
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_real_cache_parents_remain_trusted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache"
            (cache / "final-qc").mkdir(parents=True)
            (cache / "observers").mkdir()
            with patch.dict(os.environ, {"ISCO_TTS_CACHE_PATH": str(cache)}, clear=False):
                self.assertTrue(sanitize_final_observer_cache_before_runtime())


if __name__ == "__main__":
    unittest.main()

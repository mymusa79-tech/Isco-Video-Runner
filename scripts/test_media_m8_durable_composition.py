from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import isco_video_agent.orchestrator as orchestrator

from scripts import media_durable_cache as durable
from scripts import media_prepared_live_cache as prepared_live
from scripts.m8_live_binding import m8_live_scope


class MediaM8DurableCompositionTests(unittest.TestCase):
    def setUp(self) -> None:
        prepared_live.reset_media_prepared_live_cache_for_tests()
        durable.reset_media_durable_cache_for_tests()

    def tearDown(self) -> None:
        prepared_live.reset_media_prepared_live_cache_for_tests()
        durable.reset_media_durable_cache_for_tests()

    @staticmethod
    def _env(root: Path) -> dict[str, str]:
        return {
            "ISCO_TTS_CACHE_PATH": str(root / "stage-cache"),
            "ISCO_APPROVED_BRIEF_SHA256": "a" * 64,
            "ISCO_ENGINE_SHA": "b" * 40,
            "GEMINI_CONTENT_MODEL": "gemini-3.7-flash",
        }

    def test_media_prepared_scope_rewraps_m8_runtime_override(self) -> None:
        """M8 replaces orchestrator.prepare_clip during produce; Media must wrap that live seam."""
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, self._env(Path(tmp)), clear=False
        ):
            durable.install_media_durable_cache()
            self.assertTrue(
                getattr(orchestrator.prepare_clip, "_isco_media_durable_prepare", False)
            )

            with m8_live_scope():
                m8_prepare = orchestrator.prepare_clip
                self.assertFalse(
                    getattr(m8_prepare, "_isco_media_durable_prepare", False),
                    "regression precondition: M8 owns the active prepare seam inside its scope",
                )
                with prepared_live.media_prepared_cache_scope():
                    active = orchestrator.prepare_clip
                    self.assertTrue(
                        getattr(active, "_isco_media_prepared_live", False),
                        "Media prepared cache must remain outermost over the active M8 renderer",
                    )
                    self.assertIs(
                        getattr(active, "_isco_media_prepared_live_original", None),
                        m8_prepare,
                    )

    def test_install_binds_live_produce_scope_for_later_render_wrappers(self) -> None:
        """Installer must enter the prepared-cache scope after outer M8/M9/etc scopes activate."""
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, self._env(Path(tmp)), clear=False
        ):
            prepared_live.install_media_prepared_live_cache()
            self.assertTrue(
                getattr(orchestrator.produce, "_isco_media_prepared_live_produce", False),
                "Media installer must bind a live produce scope, not only a static prepare_clip patch",
            )

    def test_live_prepared_hit_restores_m8_sidecar_and_skips_renderer(self) -> None:
        """A durable hit must preserve M8 provenance while avoiding its expensive rerender."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls = {"render": 0}
            record = SimpleNamespace(
                provider="pexels",
                source_url="https://videos.pexels.com/video-files/999/999-hd.mp4",
                sha256="c" * 64,
            )
            src = root / "raw.mp4"
            src.write_bytes(b"R" * 4096)

            def renderer(source: Path, dest: Path, seconds: float, portrait: bool, fps: int = 30) -> Path:
                del source, seconds, portrait, fps
                calls["render"] += 1
                dest = Path(dest)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(b"P" * 4096)
                dest.with_suffix(".m8.json").write_text(
                    json.dumps({"status": "applied", "production_stage": "technical_normalization_before_creative_grade"}),
                    encoding="utf-8",
                )
                return dest

            with patch.dict(os.environ, self._env(root), clear=False), patch.object(
                prepared_live.trust, "trusted_record", return_value=record
            ), patch.object(orchestrator, "duration", return_value=12.0):
                cache_root = prepared_live._cache_root()
                assert cache_root is not None
                wrapped = prepared_live._wrap_prepare_clip(cache_root, renderer)
                first = wrapped(src, root / "run1" / "clip.mp4", 12.0, False, fps=30)
                self.assertEqual(first.read_bytes(), b"P" * 4096)
                self.assertTrue(first.with_suffix(".m8.json").is_file())

                second = wrapped(src, root / "run2" / "clip.mp4", 12.0, False, fps=30)
                self.assertEqual(second.read_bytes(), b"P" * 4096)
                self.assertTrue(second.with_suffix(".m8.json").is_file())
                self.assertEqual(calls["render"], 1)


if __name__ == "__main__":
    unittest.main()

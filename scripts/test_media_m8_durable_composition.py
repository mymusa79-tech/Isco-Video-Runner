from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import isco_video_agent.orchestrator as orchestrator

from scripts import media_durable_cache as durable
from scripts.m8_live_binding import m8_live_scope


class MediaM8DurableCompositionTests(unittest.TestCase):
    def setUp(self) -> None:
        durable.reset_media_durable_cache_for_tests()

    def tearDown(self) -> None:
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
                with durable.media_prepared_cache_scope():
                    active = orchestrator.prepare_clip
                    self.assertTrue(
                        getattr(active, "_isco_media_durable_prepare", False),
                        "Media prepared cache must remain outermost over the active M8 renderer",
                    )
                    self.assertIs(
                        getattr(active, "_isco_media_durable_original", None),
                        m8_prepare,
                    )

    def test_install_binds_live_produce_scope_for_later_render_wrappers(self) -> None:
        """Installer must enter the prepared-cache scope after outer M8/M9/etc scopes activate."""
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, self._env(Path(tmp)), clear=False
        ):
            durable.install_media_durable_cache()
            self.assertTrue(
                getattr(orchestrator.produce, "_isco_media_durable_produce", False),
                "Media installer must bind a live produce scope, not only a static prepare_clip patch",
            )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import isco_video_agent.orchestrator as orchestrator

from scripts import durable_stage_cache
from scripts import render_durable_cache as render


class RenderDurableContractTests(unittest.TestCase):
    @staticmethod
    def _git_blob_sha(path: str) -> str:
        payload = Path(path).read_bytes()
        return hashlib.sha1(f"blob {len(payload)}\0".encode("ascii") + payload).hexdigest()

    def test_previous_durable_semantic_layers_remain_byte_identical(self) -> None:
        self.assertEqual(
            self._git_blob_sha("scripts/tts_durable_cache_semantics.py"),
            "518ce3cdd0a86399de24674ad458a8d6c84f9c12",
        )
        self.assertEqual(
            self._git_blob_sha("scripts/media_durable_cache.py"),
            "4749851eddc2de8550e2232893a2b588b7359846",
        )
        self.assertEqual(
            self._git_blob_sha("scripts/media_prepared_live_cache.py"),
            "29cf487af070e83bf94e31651b48ddfde7513d99",
        )
        self.assertEqual(
            self._git_blob_sha("scripts/media_search_durable_cache.py"),
            "be20ecdafbe667e04723172b684cbb76ab0fe808",
        )

    def test_runtime_install_order_keeps_audio_integrity_outside_render_and_cinematic_scopes_inside(self) -> None:
        source = Path("scripts/runtime_closure.py").read_text(encoding="utf-8")
        audio = source.index("    install_audio_semantic_integrity_binding()")
        render_install = source.index("    install_render_durable_cache()")
        mastering = source.index("    install_audio_mastering_live_binding()")
        sfx = source.index("    install_sfx_live_binding()")
        m8 = source.index("    install_m8_live_binding()")
        m9 = source.index("    install_m9_live_binding()")
        m10 = source.index("    install_m10_live_binding()")
        cta = source.index("    install_cta_live_binding()")
        self.assertLess(audio, render_install)
        for later in (mastering, sfx, m8, m9, m10, cta):
            self.assertLess(render_install, later)

    def test_scope_wraps_current_live_seams_not_static_engine_functions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {
                "ISCO_TTS_CACHE_PATH": str(Path(tmp) / "stage-cache"),
                "ISCO_APPROVED_BRIEF_SHA256": "a" * 64,
                "ISCO_ENGINE_SHA": "b" * 40,
            },
            clear=False,
        ):
            old_concat = orchestrator.concat_video
            old_burn = orchestrator.burn_srt
            old_mux = orchestrator.mux

            def active_concat(inputs, output):
                return old_concat(inputs, output)

            def active_burn(video, srt, output, *, portrait=False):
                return old_burn(video, srt, output, portrait=portrait)

            def active_mux(video, narration, output, music=None, **kwargs):
                return old_mux(video, narration, output, music=music, **kwargs)

            orchestrator.concat_video = active_concat
            orchestrator.burn_srt = active_burn
            orchestrator.mux = active_mux
            try:
                with render.render_durable_scope():
                    self.assertIs(
                        getattr(orchestrator.concat_video, "_isco_render_durable_original", None),
                        active_concat,
                    )
                    self.assertIs(
                        getattr(orchestrator.burn_srt, "_isco_render_durable_original", None),
                        active_burn,
                    )
                    self.assertIs(
                        getattr(orchestrator.mux, "_isco_render_durable_original", None),
                        active_mux,
                    )
            finally:
                orchestrator.concat_video = old_concat
                orchestrator.burn_srt = old_burn
                orchestrator.mux = old_mux

    def test_shared_transport_can_save_render_without_requiring_tts_or_media_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "stage-cache"
            render_root = root / "render"
            render_root.mkdir(parents=True, exist_ok=True)

            # This test isolates transport ownership. The Render namespace validator is
            # patched to represent a prevalidated Render entry; TTS/Media remain empty.
            with patch(
                "scripts.render_durable_cache.prepare_cache_for_persistence",
                return_value=True,
            ):
                self.assertTrue(durable_stage_cache.prepare_cache_for_persistence(root))


if __name__ == "__main__":
    unittest.main()

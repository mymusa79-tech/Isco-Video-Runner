from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import durable_stage_cache


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

    def test_runtime_order_keeps_render_inside_cinematic_wrappers(self) -> None:
        source = Path("scripts/runtime_closure.py").read_text(encoding="utf-8")
        audio = source.index("    install_audio_semantic_integrity_binding()")
        mastering = source.index("    install_audio_mastering_live_binding()")
        cinematic = source.index(
            "    install_cinematic_runtime_port(CinematicInstallPhase.INNER)"
        )
        render = source.index("    install_render_runtime_port()")
        narrative = source.index("    install_narrative_music_dynamics()")
        self.assertLess(audio, mastering)
        self.assertLess(mastering, cinematic)
        self.assertLess(cinematic, render)
        self.assertLess(render, narrative)
        self.assertNotIn("    install_render_durable_cache()", source)
        for direct in (
            "    install_sfx_live_binding()",
            "    install_m8_live_binding()",
            "    install_m9_live_binding()",
            "    install_m10_live_binding()",
            "    install_cta_live_binding()",
        ):
            self.assertNotIn(direct, source)

    def test_render_never_replays_cinematic_reports_or_caches_copy_concat(self) -> None:
        source = Path("scripts/render_durable_cache.py").read_text(encoding="utf-8")
        self.assertIn("_wrap_m9_pair", source)
        self.assertNotIn("def _wrap_concat", source)
        self.assertNotIn("_FINAL_SIDECARS", source)
        self.assertNotIn("m10-cards.json", source)
        self.assertNotIn("cta-plan.json", source)
        self.assertNotIn("narrative-music-dynamics.json", source)
        self.assertIn("render_cache_install_order_must_precede_narrative_music_dynamics", source)

    def test_render_binding_is_semantically_isolated_from_transport_code(self) -> None:
        source = Path("scripts/render_durable_cache.py").read_text(encoding="utf-8")
        self.assertNotIn("render_contract_sha256", source)
        self.assertNotIn("_module_sha(sys.modules[__name__])", source)
        self.assertIn('CACHE_NAMESPACE = "render-durable-v2"', source)
        self.assertIn('"ffmpeg": ffmpeg', source)
        self.assertIn('"font": font', source)

    def test_shared_transport_can_save_render_without_tts_or_media_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "stage-cache"
            (root / "render").mkdir(parents=True, exist_ok=True)
            with patch(
                "scripts.render_durable_cache.prepare_cache_for_persistence",
                return_value=True,
            ):
                self.assertTrue(durable_stage_cache.prepare_cache_for_persistence(root))


if __name__ == "__main__":
    unittest.main()

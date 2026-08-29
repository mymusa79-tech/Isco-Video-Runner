from __future__ import annotations

import os
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import scripts.m7_live_binding as bridge


class M7RunnerInstallerTests(unittest.TestCase):
    def tearDown(self) -> None:
        current = bridge.orchestrator.produce
        original = getattr(current, "_isco_m7_original", None)
        if original is not None:
            bridge.orchestrator.produce = original

    def test_installer_is_idempotent_captures_keys_and_chains_security(self) -> None:
        calls = []

        def core(*args, **kwargs):
            calls.append(("core", args, kwargs))
            return "out"

        @contextmanager
        def scope(module, *, pexels_api_key, pixabay_api_key):
            calls.append(("scope", pexels_api_key, pixabay_api_key, module is bridge.orchestrator))
            yield

        with patch.object(bridge.orchestrator, "produce", core), patch.object(
            bridge, "live_m7_binding_scope", scope
        ), patch.object(
            bridge, "install_security_v1_live_binding"
        ) as security, patch.dict(
            os.environ,
            {"PEXELS_API_KEY": "pexels-secret", "PIXABAY_API_KEY": "pixabay-secret"},
            clear=False,
        ):
            bridge.install_m7_live_binding()
            wrapped = bridge.orchestrator.produce
            bridge.install_m7_live_binding()
            self.assertIs(bridge.orchestrator.produce, wrapped)
            result = wrapped(topic="x")

        self.assertEqual(result, "out")
        self.assertEqual(calls[0], ("scope", "pexels-secret", "pixabay-secret", True))
        self.assertEqual(calls[1][0], "core")
        self.assertEqual(security.call_count, 2)

    def test_missing_pexels_preserves_core_authoritative_failure_path(self) -> None:
        calls = []

        def core(*args, **kwargs):
            calls.append("core")
            raise RuntimeError("missing pexels from core")

        with patch.object(bridge.orchestrator, "produce", core), patch.object(
            bridge, "install_security_v1_live_binding"
        ), patch.dict(
            os.environ, {"PEXELS_API_KEY": "", "PIXABAY_API_KEY": ""}, clear=False
        ):
            bridge.install_m7_live_binding()
            with self.assertRaisesRegex(RuntimeError, "missing pexels from core"):
                bridge.orchestrator.produce()
        self.assertEqual(calls, ["core"])

    def test_m11_security_firewall_blocks_before_vision_budget_or_cloud_review(self) -> None:
        ledger = MagicMock()
        audit = MagicMock()
        candidate = SimpleNamespace(provider=SimpleNamespace(value="the_met"), object_id="42")
        review = bridge._m11_review_fn(
            output_dir=Path("out"),
            gemini_api_key="gemini",
            content_model="gemini-2.5-flash",
            ledger=ledger,
            audit_fn=audit,
        )
        with patch.object(
            bridge.security_v1,
            "_scan_media_before_vision",
            side_effect=RuntimeError(
                bridge.security_v1._FIREWALL_BLOCK_PREFIX + "prompt_like_text_detected"
            ),
        ):
            result = review(Path("archive.jpg"), {"body_index": 0}, candidate)

        self.assertEqual(result["status"], "block")
        self.assertEqual(result["local_media_rejection"], "prompt_like_text_detected")
        ledger.register_task.assert_not_called()
        ledger.authorize.assert_not_called()
        audit.assert_not_called()

    def test_m11_archive_render_reenters_live_prepare_clip_color_authority(self) -> None:
        runtime = SimpleNamespace(_render_archive_clip=MagicMock())
        render = bridge._m11_color_authority_render_fn(runtime)
        destination = Path("archive-final.mp4")
        expected_raw = Path("archive-final.m11-pre-color.mp4")
        with patch.object(
            bridge.media_ffmpeg, "prepare_clip", return_value=destination
        ) as prepare:
            result = render(Path("archive.jpg"), destination, 7.5, fps=30)

        self.assertEqual(result, destination)
        runtime._render_archive_clip.assert_called_once_with(
            Path("archive.jpg"), expected_raw, 7.5, fps=30
        )
        prepare.assert_called_once_with(expected_raw, destination, 7.5, False, 30)


if __name__ == "__main__":
    unittest.main()

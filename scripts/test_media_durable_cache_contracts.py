from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import isco_video_agent.orchestrator as orchestrator
import isco_video_agent.visual_selection as visual_selection

from scripts import media_durable_cache as durable
from scripts import media_trust_boundary_v2 as trust


class MediaDurableCacheContractTests(unittest.TestCase):
    def setUp(self) -> None:
        durable.reset_media_durable_cache_for_tests()
        trust.reset_media_trust_state_for_tests()
        durable._LOCAL_REVALIDATED_RAW.clear()

    def tearDown(self) -> None:
        durable.reset_media_durable_cache_for_tests()
        trust.reset_media_trust_state_for_tests()
        durable._LOCAL_REVALIDATED_RAW.clear()

    def _env(self, root: Path) -> dict[str, str]:
        return {
            "ISCO_TTS_CACHE_PATH": str(root / "stage-cache"),
            "ISCO_APPROVED_BRIEF_SHA256": "a" * 64,
            "ISCO_ENGINE_SHA": "b" * 40,
            "GEMINI_CONTENT_MODEL": "gemini-3.7-flash",
        }

    def test_media_namespace_derives_from_shared_durable_stage_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict(os.environ, self._env(root), clear=False):
                os.environ.pop("ISCO_MEDIA_CACHE_PATH", None)
                self.assertEqual(durable._cache_root(), root / "stage-cache" / "media")

    def test_reset_never_resurrects_an_external_test_patch_after_context_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline_download = trust.trusted_download

            def external_download(provider: str, url: str, dest: Path) -> Path:
                del provider, url
                Path(dest).parent.mkdir(parents=True, exist_ok=True)
                Path(dest).write_bytes(b"external")
                return Path(dest)

            with patch.dict(os.environ, self._env(root), clear=False), patch.object(
                trust, "trusted_download", external_download
            ):
                durable.install_media_durable_cache()
                self.assertTrue(getattr(trust.trusted_download, "_isco_media_durable_raw", False))

            # unittest.mock has now restored the canonical function. reset() must clear
            # durable ownership without overwriting that restoration with external_download.
            self.assertIs(trust.trusted_download, baseline_download)
            durable.reset_media_durable_cache_for_tests()
            self.assertIs(trust.trusted_download, baseline_download)

    def test_cached_raw_revalidation_runs_stock_preflight_and_distributed_security(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "cached.mp4"
            media.write_bytes(b"M" * 4096)
            with patch.object(trust, "_inspect_exact_review_source", return_value=None) as inspect, patch.object(
                trust, "_distributed_scan_media_before_vision", return_value=None
            ) as distributed:
                self.assertTrue(durable._revalidate_cached_raw("a" * 64, media))
                inspect.assert_called_once_with(media)
                distributed.assert_called_once_with(media)
                # Same exact raw bytes are revalidated once per live process, not once
                # per downstream consumer.
                self.assertTrue(durable._revalidate_cached_raw("a" * 64, media))
                self.assertEqual(inspect.call_count, 1)
                self.assertEqual(distributed.call_count, 1)

    def test_security_rejection_discards_cached_raw_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "cached.mp4"
            media.write_bytes(b"M" * 4096)
            with patch.object(trust, "_inspect_exact_review_source", return_value=None), patch.object(
                trust,
                "_distributed_scan_media_before_vision",
                side_effect=RuntimeError("security block"),
            ):
                self.assertFalse(durable._revalidate_cached_raw("b" * 64, media))
                self.assertNotIn("b" * 64, durable._LOCAL_REVALIDATED_RAW)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import hashlib
from pathlib import Path
import unittest
from unittest.mock import patch

from scripts import orchestration_media_port as port
from scripts.orchestration_stage_registry import build_l4_registry

# Certification sync marker: test-only change to trigger main-filtered PR workflows.


class MediaStablePortTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calls: list[str] = []
        self.flags = {
            "provider": port.provider_capacity_v2._INSTALLED,
            "trust": port.media_trust_boundary_v2._INSTALLED,
            "durable": port.media_durable_cache._INSTALLED,
            "prepared": port.media_prepared_live_cache._INSTALLED,
            "search": port.media_search_durable_cache._INSTALLED,
        }
        port.provider_capacity_v2._INSTALLED = False
        port.media_trust_boundary_v2._INSTALLED = False
        port.media_durable_cache._INSTALLED = False
        port.media_prepared_live_cache._INSTALLED = False
        port.media_search_durable_cache._INSTALLED = False

    def tearDown(self) -> None:
        port.provider_capacity_v2._INSTALLED = self.flags["provider"]
        port.media_trust_boundary_v2._INSTALLED = self.flags["trust"]
        port.media_durable_cache._INSTALLED = self.flags["durable"]
        port.media_prepared_live_cache._INSTALLED = self.flags["prepared"]
        port.media_search_durable_cache._INSTALLED = self.flags["search"]

    def _installer(self, name: str, module, *, install: bool = True):
        def run() -> None:
            if not module._INSTALLED:
                self.calls.append(name)
                if install:
                    module._INSTALLED = True
        return run

    def _patch_installers(self, *, cache_root: Path | None):
        return (
            patch.object(
                port.provider_capacity_v2,
                "install_provider_capacity_v2",
                side_effect=self._installer("provider-capacity", port.provider_capacity_v2),
            ),
            patch.object(
                port.media_trust_boundary_v2,
                "install_media_trust_boundary_v2",
                side_effect=self._installer("media-trust", port.media_trust_boundary_v2),
            ),
            patch.object(
                port.media_durable_cache,
                "install_media_durable_cache",
                side_effect=self._installer("durable-asset", port.media_durable_cache, install=cache_root is not None),
            ),
            patch.object(
                port.media_prepared_live_cache,
                "install_media_prepared_live_cache",
                side_effect=self._installer("prepared-live", port.media_prepared_live_cache, install=cache_root is not None),
            ),
            patch.object(
                port.media_search_durable_cache,
                "install_media_search_durable_cache",
                side_effect=self._installer("search", port.media_search_durable_cache, install=cache_root is not None),
            ),
            patch.object(port.media_durable_cache, "_cache_root", return_value=cache_root),
        )

    def test_port_preserves_historical_order_and_is_idempotent(self) -> None:
        patches = self._patch_installers(cache_root=Path("/tmp/media-cache"))
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            first = port.install_media_runtime_port()
            second = port.install_media_runtime_port()

        self.assertEqual(
            self.calls,
            ["provider-capacity", "media-trust", "durable-asset", "prepared-live", "search"],
        )
        self.assertEqual(first, second)
        self.assertTrue(first.provider_capacity_installed)
        self.assertTrue(first.trust_boundary_installed)
        self.assertTrue(first.durable_cache_configured)
        self.assertTrue(first.durable_asset_cache_installed)
        self.assertTrue(first.prepared_live_cache_installed)
        self.assertTrue(first.search_cache_installed)
        self.assertEqual(first.provider_owner, "media-trust-security-core")
        self.assertEqual(first.retry_owner, "media-trust-security-core")

    def test_durable_layers_remain_optional_when_cache_root_is_not_configured(self) -> None:
        patches = self._patch_installers(cache_root=None)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            evidence = port.install_media_runtime_port()

        self.assertEqual(
            self.calls,
            ["provider-capacity", "media-trust", "durable-asset", "prepared-live", "search"],
        )
        self.assertFalse(evidence.durable_cache_configured)
        self.assertFalse(evidence.durable_asset_cache_installed)
        self.assertFalse(evidence.prepared_live_cache_installed)
        self.assertFalse(evidence.search_cache_installed)

    def test_missing_required_trust_boundary_fails_closed_before_caches(self) -> None:
        with patch.object(
            port.provider_capacity_v2,
            "install_provider_capacity_v2",
            side_effect=self._installer("provider-capacity", port.provider_capacity_v2),
        ), patch.object(
            port.media_trust_boundary_v2,
            "install_media_trust_boundary_v2",
            side_effect=self._installer("media-trust", port.media_trust_boundary_v2, install=False),
        ), patch.object(port.media_durable_cache, "install_media_durable_cache") as durable:
            with self.assertRaises(port.MediaRuntimePortError):
                port.install_media_runtime_port()
        durable.assert_not_called()

    def test_configured_durable_layer_missing_fails_closed(self) -> None:
        patches = self._patch_installers(cache_root=Path("/tmp/media-cache"))
        bad = patch.object(
            port.media_search_durable_cache,
            "install_media_search_durable_cache",
            side_effect=self._installer("search", port.media_search_durable_cache, install=False),
        )
        with patches[0], patches[1], patches[2], patches[3], bad, patches[5]:
            with self.assertRaises(port.MediaRuntimePortError):
                port.install_media_runtime_port()

    def test_stage_registry_binds_media_to_exact_port_blob_and_preserves_owners(self) -> None:
        contract = build_l4_registry().get("media")
        binding = contract.implementation_binding
        data = Path("scripts/orchestration_media_port.py").read_bytes()
        actual_blob = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()

        self.assertEqual(binding.adapter_id, port.PORT_ID)
        self.assertEqual(binding.source_path, "scripts/orchestration_media_port.py")
        self.assertEqual(binding.source_sha, actual_blob)
        self.assertEqual(contract.provider_policy["owner"], port.PROVIDER_OWNER)
        self.assertEqual(contract.retry_policy.owner, port.RETRY_OWNER)
        self.assertTrue(contract.retry_policy.bounded)
        self.assertTrue(contract.cache_policy.read)
        self.assertTrue(contract.cache_policy.write)
        self.assertTrue(contract.cache_policy.write_after_validation)
        self.assertTrue(contract.cache_policy.revalidate_hits)

    def test_runtime_closure_uses_only_the_stable_media_install_seam(self) -> None:
        source = Path("scripts/runtime_closure.py").read_text(encoding="utf-8")
        self.assertIn("from scripts.orchestration_media_port import install_media_runtime_port", source)
        self.assertEqual(source.count("install_media_runtime_port()"), 1)
        for direct in (
            "install_provider_capacity_v2()",
            "install_media_trust_boundary_v2()",
            "install_media_durable_cache()",
            "install_media_prepared_live_cache()",
            "install_media_search_durable_cache()",
        ):
            self.assertNotIn(direct, source)

    def test_port_does_not_take_provider_retry_or_cache_execution_ownership(self) -> None:
        source = Path("scripts/orchestration_media_port.py").read_text(encoding="utf-8")
        for forbidden in (
            "requests.",
            "time.sleep",
            "os.replace",
            "json.dump",
            "trusted_download(",
            "get_or_fetch(",
        ):
            self.assertNotIn(forbidden, source)
        self.assertIn('PROVIDER_OWNER = "media-trust-security-core"', source)
        self.assertIn('RETRY_OWNER = "media-trust-security-core"', source)


if __name__ == "__main__":
    unittest.main()

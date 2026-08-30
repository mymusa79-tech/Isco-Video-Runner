from __future__ import annotations

import hashlib
from pathlib import Path
import unittest
from unittest.mock import patch

from scripts import orchestration_render_port as port
from scripts.orchestration_stage_registry import build_l4_registry


class RenderStablePortTests(unittest.TestCase):
    def test_configured_cache_installs_and_reports_existing_owner_contract(self) -> None:
        with patch.object(port.render_durable_cache, "_shared_root", return_value=Path("/tmp/render")), \
             patch.object(port.render_durable_cache, "_INSTALLED", False), \
             patch.object(
                 port.render_durable_cache,
                 "install_render_durable_cache",
                 side_effect=lambda: setattr(port.render_durable_cache, "_INSTALLED", True),
             ) as installer:
            evidence = port.install_render_runtime_port()

        installer.assert_called_once_with()
        self.assertTrue(evidence.durable_cache_configured)
        self.assertTrue(evidence.durable_cache_installed)
        self.assertEqual(evidence.cache_namespace, port.render_durable_cache.CACHE_NAMESPACE)
        self.assertEqual(evidence.cache_schema_version, port.render_durable_cache.CACHE_SCHEMA_VERSION)
        self.assertTrue(evidence.current_qc_revalidation_required)

    def test_unconfigured_cache_is_valid_disabled_state(self) -> None:
        with patch.object(port.render_durable_cache, "_shared_root", return_value=None), \
             patch.object(port.render_durable_cache, "_INSTALLED", False), \
             patch.object(port.render_durable_cache, "install_render_durable_cache") as installer:
            evidence = port.install_render_runtime_port()

        installer.assert_called_once_with()
        self.assertFalse(evidence.durable_cache_configured)
        self.assertFalse(evidence.durable_cache_installed)

    def test_configured_cache_fails_loud_if_owner_does_not_install(self) -> None:
        with patch.object(port.render_durable_cache, "_shared_root", return_value=Path("/tmp/render")), \
             patch.object(port.render_durable_cache, "_INSTALLED", False), \
             patch.object(port.render_durable_cache, "install_render_durable_cache"):
            with self.assertRaises(port.RenderRuntimePortError):
                port.install_render_runtime_port()

    def test_stage_registry_binds_render_to_exact_port_blob_and_preserves_owners(self) -> None:
        contract = build_l4_registry().get("render")
        binding = contract.implementation_binding
        data = Path("scripts/orchestration_render_port.py").read_bytes()
        actual_blob = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()

        self.assertEqual(binding.adapter_id, port.PORT_ID)
        self.assertEqual(binding.source_path, "scripts/orchestration_render_port.py")
        self.assertEqual(binding.source_sha, actual_blob)
        self.assertEqual(contract.provider_policy["owner"], port.PROVIDER_OWNER)
        self.assertEqual(contract.retry_policy.owner, port.RETRY_OWNER)
        self.assertTrue(contract.cache_policy.read)
        self.assertTrue(contract.cache_policy.write)
        self.assertEqual(contract.side_effect_policy, "idempotent")

    def test_runtime_closure_uses_only_render_stable_seam_in_historical_position(self) -> None:
        source = Path("scripts/runtime_closure.py").read_text(encoding="utf-8")
        marker = "install_render_runtime_port()"
        self.assertEqual(source.count(marker), 1)
        self.assertLess(
            source.index("install_cinematic_runtime_port(CinematicInstallPhase.INNER)"),
            source.index(marker),
        )
        self.assertLess(source.index(marker), source.index("install_narrative_music_dynamics()"))
        self.assertNotIn("install_render_durable_cache()", source)

    def test_existing_render_core_remains_exact_byte_owner(self) -> None:
        data = Path("scripts/render_durable_cache.py").read_bytes()
        actual_blob = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
        self.assertEqual(actual_blob, "abc92b472373cada7b92a7a53007ae943de98b27")

    def test_port_owns_composition_only_not_render_cache_or_quality_logic(self) -> None:
        source = Path("scripts/orchestration_render_port.py").read_text(encoding="utf-8")
        for forbidden in (
            "subprocess.",
            "shutil.",
            "_persist_entry(",
            "_restore_entry(",
            "_quality_passed(",
            "_reconcile_final_candidates(",
            "run_final_master_qc(",
        ):
            self.assertNotIn(forbidden, source)
        self.assertIn('PROVIDER_OWNER = "render-durable-core"', source)
        self.assertIn('RETRY_OWNER = "render-durable-core"', source)
        self.assertIn('CACHE_OWNER = "render-durable-core"', source)


if __name__ == "__main__":
    unittest.main()

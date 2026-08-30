from __future__ import annotations

import hashlib
from pathlib import Path
import unittest
from unittest.mock import patch

from scripts import orchestration_cinematic_port as port
from scripts.orchestration_stage_registry import build_l4_registry


class CinematicStablePortTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calls: list[str] = []
        self.modules = (
            port.sfx_live_binding,
            port.m8_live_binding,
            port.m9_live_binding,
            port.m10_live_binding,
            port.cta_live_binding,
            port.m7_live_binding,
        )
        self.original_flags = [module._INSTALLED for module in self.modules]
        for module in self.modules:
            module._INSTALLED = False

    def tearDown(self) -> None:
        for module, flag in zip(self.modules, self.original_flags):
            module._INSTALLED = flag

    def _installer(self, label: str, module, *, install: bool = True):
        def run() -> None:
            if not module._INSTALLED:
                self.calls.append(label)
                if install:
                    module._INSTALLED = True
        return run

    def _patch_inner(self):
        return (
            patch.object(port.sfx_live_binding, "install_sfx_live_binding", side_effect=self._installer("sfx", port.sfx_live_binding)),
            patch.object(port.m8_live_binding, "install_m8_live_binding", side_effect=self._installer("m8", port.m8_live_binding)),
            patch.object(port.m9_live_binding, "install_m9_live_binding", side_effect=self._installer("m9", port.m9_live_binding)),
            patch.object(port.m10_live_binding, "install_m10_live_binding", side_effect=self._installer("m10", port.m10_live_binding)),
            patch.object(port.cta_live_binding, "install_cta_live_binding", side_effect=self._installer("cta", port.cta_live_binding)),
        )

    def test_inner_phase_preserves_historical_order_and_is_idempotent(self) -> None:
        patches = self._patch_inner()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            first = port.install_cinematic_runtime_port(port.CinematicInstallPhase.INNER)
            second = port.install_cinematic_runtime_port(port.CinematicInstallPhase.INNER)

        self.assertEqual(self.calls, ["sfx", "m8", "m9", "m10", "cta"])
        self.assertEqual(first, second)
        self.assertTrue(first.sfx_installed)
        self.assertTrue(first.m8_installed)
        self.assertTrue(first.m9_installed)
        self.assertTrue(first.m10_installed)
        self.assertTrue(first.cta_installed)
        self.assertFalse(first.m7_m11_installed)

    def test_outer_phase_preserves_existing_m7_owned_m11_composition(self) -> None:
        with patch.object(
            port.m7_live_binding,
            "install_m7_live_binding",
            side_effect=self._installer("m7-m11", port.m7_live_binding),
        ):
            first = port.install_cinematic_runtime_port(port.CinematicInstallPhase.OUTER)
            second = port.install_cinematic_runtime_port("outer")

        self.assertEqual(self.calls, ["m7-m11"])
        self.assertEqual(first, second)
        self.assertTrue(first.m7_m11_installed)
        self.assertFalse(first.sfx_installed)

    def test_inner_phase_fails_loud_before_downstream_layers_if_required_layer_missing(self) -> None:
        with patch.object(
            port.sfx_live_binding,
            "install_sfx_live_binding",
            side_effect=self._installer("sfx", port.sfx_live_binding, install=False),
        ), patch.object(port.m8_live_binding, "install_m8_live_binding") as m8:
            with self.assertRaises(port.CinematicRuntimePortError):
                port.install_cinematic_runtime_port(port.CinematicInstallPhase.INNER)
        m8.assert_not_called()

    def test_outer_phase_fails_loud_if_m7_m11_does_not_install(self) -> None:
        with patch.object(
            port.m7_live_binding,
            "install_m7_live_binding",
            side_effect=self._installer("m7-m11", port.m7_live_binding, install=False),
        ):
            with self.assertRaises(port.CinematicRuntimePortError):
                port.install_cinematic_runtime_port(port.CinematicInstallPhase.OUTER)

    def test_unknown_phase_fails_loud_without_installing_anything(self) -> None:
        with self.assertRaises(port.CinematicRuntimePortError):
            port.install_cinematic_runtime_port("render")
        self.assertEqual(self.calls, [])

    def test_stage_registry_binds_cinematic_to_exact_port_blob_and_preserves_owners(self) -> None:
        contract = build_l4_registry().get("cinematic")
        binding = contract.implementation_binding
        data = Path("scripts/orchestration_cinematic_port.py").read_bytes()
        actual_blob = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()

        self.assertEqual(binding.adapter_id, port.PORT_ID)
        self.assertEqual(binding.source_path, "scripts/orchestration_cinematic_port.py")
        self.assertEqual(binding.source_sha, actual_blob)
        self.assertEqual(contract.provider_policy["owner"], port.PROVIDER_OWNER)
        self.assertEqual(contract.retry_policy.owner, port.RETRY_OWNER)
        self.assertFalse(contract.cache_policy.read)
        self.assertFalse(contract.cache_policy.write)
        self.assertEqual(contract.side_effect_policy, "none")

    def test_runtime_closure_uses_only_inner_stable_seam_before_render(self) -> None:
        source = Path("scripts/runtime_closure.py").read_text(encoding="utf-8")
        marker = "install_cinematic_runtime_port(CinematicInstallPhase.INNER)"
        self.assertEqual(source.count(marker), 1)
        self.assertLess(source.index(marker), source.index("install_render_durable_cache()"))
        for direct in (
            "install_sfx_live_binding()",
            "install_m8_live_binding()",
            "install_m9_live_binding()",
            "install_m10_live_binding()",
            "install_cta_live_binding()",
        ):
            self.assertNotIn(direct, source)

    def test_entrypoint_uses_only_outer_stable_seam_in_historical_position(self) -> None:
        source = Path("scripts/run_v3_voice.py").read_text(encoding="utf-8")
        outer = "install_cinematic_runtime_port(CinematicInstallPhase.OUTER)"
        self.assertEqual(source.count(outer), 1)
        self.assertLess(source.index("install_tts_runtime_port()"), source.index(outer))
        self.assertLess(source.index(outer), source.index("install_opening_feasibility_guard()"))
        self.assertNotIn("install_m7_live_binding()", source)

    def test_port_owns_composition_only_not_provider_retry_render_or_quality_logic(self) -> None:
        source = Path("scripts/orchestration_cinematic_port.py").read_text(encoding="utf-8")
        for forbidden in (
            "requests.",
            "time.sleep",
            "subprocess.",
            "ffmpeg",
            "search_videos(",
            "search_photos(",
            "run_final_master_qc(",
            "run_gold_enforce_phase4(",
        ):
            self.assertNotIn(forbidden, source)
        self.assertIn('PROVIDER_OWNER = "certified-cinematic-core"', source)
        self.assertIn('RETRY_OWNER = "certified-cinematic-core"', source)


if __name__ == "__main__":
    unittest.main()

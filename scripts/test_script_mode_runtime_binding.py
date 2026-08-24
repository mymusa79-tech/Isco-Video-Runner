from __future__ import annotations

import sys
import unittest
from types import ModuleType
from unittest.mock import patch

import scripts.run_v3_voice as package_entrypoint
import scripts.runtime_closure as closure
import scripts.runtime_reliability as reliability


class ScriptModeRuntimeBindingTests(unittest.TestCase):
    def test_real_script_file_is_detected_as_second_live_entrypoint_module(self) -> None:
        fake_main = ModuleType("__main__")
        fake_main.__file__ = package_entrypoint.__file__
        with patch.dict(sys.modules, {"__main__": fake_main}):
            modules = reliability.production_entrypoint_modules()
        self.assertEqual(modules[0], package_entrypoint)
        self.assertIn(fake_main, modules)
        self.assertEqual(len(modules), 2)

    @staticmethod
    def _fake_entrypoint(name: str) -> ModuleType:
        module = ModuleType(name)

        def manifest(out, *, production_id, fmt):
            del out, production_id, fmt
            return {"ok": True}

        def gold(*args, **kwargs):
            del args, kwargs
            return object(), {"status": "pass"}, {}

        def telemetry(out_dir):
            del out_dir
            raise AssertionError("telemetry body is not called by this binding-only test")

        module._write_production_manifest = manifest
        module.run_gold_enforce_phase4 = gold
        module.write_planning_telemetry = telemetry
        return module

    def test_canonical_release_and_telemetry_wrappers_bind_both_module_identities(self) -> None:
        package = self._fake_entrypoint("scripts.run_v3_voice")
        script = self._fake_entrypoint("__main__")
        modules = [package, script]
        with patch.object(closure, "production_entrypoint_modules", return_value=modules):
            closure.install_canonical_v4_bundle_post_manifest()
        with patch.object(reliability, "production_entrypoint_modules", return_value=modules):
            reliability.install_release_transaction_guard()
            reliability.install_telemetry_reliability_binding()

        for module in modules:
            with self.subTest(module=module.__name__):
                reliability._assert_entrypoint_module_contract(module)
                manifest = module._write_production_manifest
                self.assertTrue(getattr(manifest, "_isco_release_transaction_delivery", False))
                canonical = manifest._isco_release_transaction_original
                self.assertTrue(getattr(canonical, "_isco_canonical_v4_bundle", False))
                self.assertTrue(
                    getattr(module.run_gold_enforce_phase4, "_isco_release_transaction_gold", False)
                )
                self.assertTrue(
                    getattr(module.write_planning_telemetry, "_isco_reliability_telemetry_binding", False)
                )


if __name__ == "__main__":
    unittest.main()

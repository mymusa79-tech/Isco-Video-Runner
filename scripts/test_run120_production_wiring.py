from __future__ import annotations

import inspect
import unittest

import scripts
from scripts import run_v3_voice


class Run120ProductionWiringTests(unittest.TestCase):
    def test_package_init_has_no_runtime_install_side_effects(self):
        source = inspect.getsource(scripts)
        self.assertNotIn("install_run120_dossier_repair_hardening", source)
        self.assertNotIn("install_run120_schema_policy_bridge", source)

    def test_run120_installers_are_explicit_and_inside_existing_quality_wrapper(self):
        source = inspect.getsource(run_v3_voice.main)
        batch = source.index("install_planning_batch_hardening()")
        schema = source.index("install_schema_repair_policy()")
        dossier = source.index("install_run120_dossier_repair_hardening()")
        bridge = source.index("install_run120_schema_policy_bridge()")
        quality = source.index("install_planner_quality_guard()")
        self.assertLess(batch, schema)
        self.assertLess(schema, dossier)
        self.assertLess(dossier, bridge)
        self.assertLess(bridge, quality)


if __name__ == "__main__":
    unittest.main()

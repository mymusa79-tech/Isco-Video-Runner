from __future__ import annotations

import unittest

import isco_video_agent.resilient_planner as staged
import scripts.append_retry_guard as append_retry_guard
import scripts.brand_anchor_guard as brand_anchor_guard
from scripts.runtime_reliability import _require_marker


class Run103RuntimeMarkerPreservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.saved_runner = append_retry_guard._repair_all_residual_underlength
        self.saved_staged = staged._script_doctor_underlength_retry

    def tearDown(self) -> None:
        append_retry_guard._repair_all_residual_underlength = self.saved_runner
        staged._script_doctor_underlength_retry = self.saved_staged

    @staticmethod
    def _base_repair(*args, **kwargs):
        del args, kwargs
        return {}

    def test_identity_isolation_preserves_existing_bounded_recovery_contract(self) -> None:
        base = self._base_repair
        base._isco_bounded_output_recovery = True
        append_retry_guard._repair_all_residual_underlength = base
        staged._script_doctor_underlength_retry = base

        brand_anchor_guard._install_append_retry_identity_isolation()

        wrapped = append_retry_guard._repair_all_residual_underlength
        self.assertTrue(getattr(wrapped, "_isco_run88_identity_isolation", False))
        self.assertTrue(getattr(wrapped, "_isco_bounded_output_recovery", False))
        self.assertIs(getattr(wrapped, "__wrapped__", None), base)
        _require_marker("append residual repair", wrapped, "_isco_bounded_output_recovery")
        self.assertIs(staged._script_doctor_underlength_retry, wrapped)

    def test_identity_isolation_does_not_invent_missing_runtime_contract(self) -> None:
        base = self._base_repair
        if hasattr(base, "_isco_bounded_output_recovery"):
            delattr(base, "_isco_bounded_output_recovery")
        append_retry_guard._repair_all_residual_underlength = base
        staged._script_doctor_underlength_retry = base

        brand_anchor_guard._install_append_retry_identity_isolation()

        wrapped = append_retry_guard._repair_all_residual_underlength
        self.assertTrue(getattr(wrapped, "_isco_run88_identity_isolation", False))
        self.assertFalse(getattr(wrapped, "_isco_bounded_output_recovery", False))


if __name__ == "__main__":
    unittest.main()

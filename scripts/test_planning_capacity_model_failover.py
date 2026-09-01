from __future__ import annotations

import unittest

from scripts import planning_capacity_profile as profile


class PlanningCapacityModelFailoverTests(unittest.TestCase):
    def test_operational_headroom_preflight_is_model_scoped_unavailability(self):
        profile.install_planning_capacity_profile()
        self.assertTrue(
            profile.run125._is_model_unavailable(
                RuntimeError(
                    "GROQ_TPM_CAPACITY_PREFLIGHT model=openai/gpt-oss-20b "
                    "estimated_total=7300 limit=8000"
                )
            )
        )

    def test_unrelated_failure_is_not_reclassified(self):
        profile.install_planning_capacity_profile()
        self.assertFalse(
            profile.run125._is_model_unavailable(
                RuntimeError("provider returned invalid JSON")
            )
        )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest

from scripts import operational_headroom_contract as headroom
from scripts import provider_capacity_hardening as capacity


MODEL = "openai/gpt-oss-120b"


class OperationalHeadroomContractTests(unittest.TestCase):
    def setUp(self) -> None:
        capacity.reset_groq_capacity_state_for_tests()

    def tearDown(self) -> None:
        capacity.reset_groq_capacity_state_for_tests()

    def test_run154_7993_of_8000_is_rejected_as_cliff_edge(self):
        state = capacity._model_state(MODEL)
        state.update(
            {
                "contacted": True,
                "actual_tpm_limit": 8000,
                "remaining_tokens": 8000,
                "reset_at_epoch": None,
                "blocked_reason": None,
            }
        )
        base = {
            "action": "admit",
            "reason": "capacity_available",
            "required_tokens": 7993,
            "actual_limit": 8000,
            "remaining_tokens": 8000,
        }
        decision = headroom.apply_operational_headroom(MODEL, 7993, base)
        self.assertEqual(decision["action"], "impossible")
        self.assertEqual(
            decision["reason"],
            "actual_limit_operational_headroom_below_required",
        )
        self.assertEqual(decision["operational_headroom_tokens"], 800)
        self.assertEqual(decision["required_with_operational_headroom"], 8793)

    def test_6500_of_8000_keeps_real_operational_margin(self):
        base = {
            "action": "admit",
            "reason": "capacity_available",
            "required_tokens": 6500,
            "actual_limit": 8000,
            "remaining_tokens": 8000,
        }
        decision = headroom.apply_operational_headroom(MODEL, 6500, base)
        self.assertEqual(decision["action"], "admit")
        self.assertEqual(decision["operational_headroom_tokens"], 800)
        self.assertEqual(decision["required_with_operational_headroom"], 7300)

    def test_remaining_window_must_cover_request_plus_headroom(self):
        base = {
            "action": "admit",
            "reason": "capacity_available",
            "required_tokens": 6500,
            "actual_limit": 8000,
            "remaining_tokens": 7000,
        }
        decision = headroom.apply_operational_headroom(MODEL, 6500, base)
        self.assertEqual(decision["action"], "wait")
        self.assertEqual(
            decision["reason"],
            "remaining_below_required_with_operational_headroom",
        )

    def test_existing_mathematical_impossible_taxonomy_is_preserved(self):
        base = {
            "action": "impossible",
            "reason": "actual_limit_below_required",
            "required_tokens": 2600,
            "actual_limit": 800,
            "remaining_tokens": 60,
        }
        decision = headroom.apply_operational_headroom(MODEL, 2600, base)
        self.assertEqual(decision["action"], "impossible")
        self.assertEqual(decision["reason"], "actual_limit_below_required")


if __name__ == "__main__":
    unittest.main()

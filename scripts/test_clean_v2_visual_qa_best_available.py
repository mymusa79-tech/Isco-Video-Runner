from __future__ import annotations

import unittest

from clean_v2.visual_qa import _can_retain_safe_best_available_primary


class VisualQABestAvailableRetentionTests(unittest.TestCase):
    def test_hook_pass_at_point_80_is_retained_when_recovery_is_no_better(self) -> None:
        self.assertTrue(
            _can_retain_safe_best_available_primary(
                primary_audit={"status": "pass"},
                primary_floor=0.80,
                best_recovery_floor=0.80,
                is_hook=True,
            )
        )

    def test_hook_below_point_80_still_fails_closed(self) -> None:
        self.assertFalse(
            _can_retain_safe_best_available_primary(
                primary_audit={"status": "pass"},
                primary_floor=0.79,
                best_recovery_floor=0.79,
                is_hook=True,
            )
        )

    def test_non_hook_keeps_existing_point_78_safe_floor(self) -> None:
        self.assertTrue(
            _can_retain_safe_best_available_primary(
                primary_audit={"status": "pass"},
                primary_floor=0.78,
                best_recovery_floor=0.78,
                is_hook=False,
            )
        )

    def test_blocked_primary_never_uses_best_available_fallback(self) -> None:
        self.assertFalse(
            _can_retain_safe_best_available_primary(
                primary_audit={"status": "block"},
                primary_floor=0.95,
                best_recovery_floor=0.70,
                is_hook=True,
            )
        )

    def test_better_recovery_prevents_retaining_primary(self) -> None:
        self.assertFalse(
            _can_retain_safe_best_available_primary(
                primary_audit={"status": "pass"},
                primary_floor=0.80,
                best_recovery_floor=0.81,
                is_hook=True,
            )
        )


if __name__ == "__main__":
    unittest.main()

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


class BestAvailableRecoveryTests(unittest.TestCase):
    def _audit(self, **extra):
        base = {
            "status": "pass",
            "observed_proof_status": "matched",
            "no_face_policy": "pass",
            "cultural_islamic_policy": "pass",
            "identifiable_person": False,
        }
        base.update(extra)
        return base

    def _ok(self, audit, floor, primary=0.5, hook=True):
        from clean_v2.visual_qa import _can_use_best_available_recovery
        return _can_use_best_available_recovery(
            audit=audit, floor=floor, primary_floor=primary, is_hook=hook
        )

    def test_run111_case_pass_matched_080_beats_blocked_primary(self):
        self.assertTrue(self._ok(self._audit(), 0.80))

    def test_hook_below_080_and_nonhook_below_078_are_refused(self):
        self.assertFalse(self._ok(self._audit(), 0.79, hook=True))
        self.assertTrue(self._ok(self._audit(), 0.78, hook=False))
        self.assertFalse(self._ok(self._audit(), 0.77, hook=False))

    def test_unproven_blocked_or_unsafe_clips_are_refused(self):
        self.assertFalse(self._ok(self._audit(observed_proof_status="missing"), 0.9))
        self.assertFalse(self._ok(self._audit(status="block"), 0.9))
        self.assertFalse(self._ok(self._audit(identifiable_person=True), 0.9))
        self.assertFalse(self._ok(self._audit(no_face_policy="block"), 0.9))
        self.assertFalse(self._ok(self._audit(cultural_islamic_policy="block"), 0.9))

    def test_must_be_strictly_better_than_primary(self):
        self.assertFalse(self._ok(self._audit(), 0.80, primary=0.80))

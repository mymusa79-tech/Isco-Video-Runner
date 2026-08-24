from __future__ import annotations

import json
import unittest
from pathlib import Path


class PreProductionRiskRegisterTests(unittest.TestCase):
    def test_register_has_required_systemic_risks(self) -> None:
        data = json.loads(Path("scripts/preproduction_risk_register.json").read_text(encoding="utf-8"))
        self.assertEqual(data["principles"]["unknown_failure"], "fail_closed")
        self.assertEqual(data["principles"]["retry_owner_count"], 1)
        self.assertFalse(data["principles"]["partial_release_allowed"])
        self.assertFalse(data["principles"]["quality_gate_relaxation_allowed"])
        ids = {item["id"] for item in data["risks"]}
        required = {
            "provider-auth-drift",
            "provider-model-drift",
            "runner-image-drift",
            "post-piper-dependency-drift",
            "release-rerun-collision",
            "system-tool-missing",
            "plaintext-secret-residue",
            "stale-checkpoint",
            "runtime-patch-identity",
            "non-idempotent-retry",
            "gemini-auth-key-september-2026",
        }
        self.assertTrue(required.issubset(ids))


if __name__ == "__main__":
    unittest.main()

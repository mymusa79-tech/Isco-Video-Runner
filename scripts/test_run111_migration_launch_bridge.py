from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "migrate-memory-and-launch-production.yml"
CRYPTO = ROOT / "scripts" / "persistent_memory_crypto.py"


class Run111MigrationLaunchBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")
        cls.crypto = CRYPTO.read_text(encoding="utf-8")

    def test_bridge_is_explicit_and_does_not_replace_canonical_production(self) -> None:
        self.assertIn("CANONICAL_PRODUCTION_WORKFLOW: produce-resilient-v4.yml", self.workflow)
        self.assertIn("EXPECTED_NEXT_PRODUCTION_RUN: \"111\"", self.workflow)
        self.assertIn("LEGACY_MIGRATION_RUN_NUMBER = \"111\"", self.crypto)
        self.assertIn("workflow_dispatch:", self.workflow)
        self.assertIn("ops/migrate-memory-and-launch", self.workflow)

    def test_exact_legacy_identity_and_sequence_are_pinned(self) -> None:
        self.assertIn(
            "LEGACY_STATE_COMMIT: 36b0db59d8c01a625c5e4e4d929e5dc298a92746",
            self.workflow,
        )
        self.assertIn("LEGACY_STATE_SEQUENCE: \"50\"", self.workflow)
        self.assertIn("--run-number \"$LEGACY_STATE_SEQUENCE\"", self.workflow)
        self.assertIn("state_sequence=50", self.workflow)

    def test_migration_is_verified_before_production_dispatch(self) -> None:
        restore = self.workflow.index("Restore and authenticate current memory")
        rewrap = self.workflow.index("Rewrap exact legacy state as authenticated AES-GCM")
        verify = self.workflow.index("Verify durable authenticated migration")
        recheck = self.workflow.index("Recheck Production sequence before dispatch")
        dispatch = self.workflow.index("Dispatch canonical Production Run 111")
        confirm = self.workflow.index("Confirm Production Run 111 exists")
        self.assertLess(restore, rewrap)
        self.assertLess(rewrap, verify)
        self.assertLess(verify, recheck)
        self.assertLess(recheck, dispatch)
        self.assertLess(dispatch, confirm)

    def test_bridge_fails_closed_on_sequence_drift(self) -> None:
        self.assertIn("Expected canonical Production latest run_number=110", self.workflow)
        self.assertGreaterEqual(self.workflow.count("test \"$latest\" -eq 110"), 1)
        self.assertIn("test \"$RESTORE_SAVE_ALLOWED\" = \"true\"", self.workflow)
        self.assertIn("agent-state still points to the legacy CBC commit", self.workflow)

    def test_bridge_has_only_required_write_permissions(self) -> None:
        self.assertIn("permissions:\n  contents: write\n  actions: write", self.workflow)
        self.assertNotIn("pull-requests: write", self.workflow)
        self.assertNotIn("issues: write", self.workflow)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.persistent_memory import PersistStatus
from scripts.state_persistence_strict import persist_strict


class StatePersistenceStrictTests(unittest.TestCase):
    def test_successful_or_unchanged_push_closes_state(self) -> None:
        for status in (
            PersistStatus(True, True, "updated"),
            PersistStatus(True, False, "unchanged"),
        ):
            with self.subTest(status=status):
                with tempfile.TemporaryDirectory() as tmp:
                    report = Path(tmp) / "state.json"
                    with patch("scripts.state_persistence_strict.persist_encrypted_state", return_value=status):
                        persist_strict(repo=Path(tmp), encrypted=Path(tmp) / "x.enc", branch="agent-state", run_number="1", report=report)
                    self.assertTrue(json.loads(report.read_text(encoding="utf-8"))["pushed"])

    def test_failed_state_push_is_a_hard_incomplete_closure(self) -> None:
        status = PersistStatus(False, True, "push rejected")
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "state.json"
            with patch("scripts.state_persistence_strict.persist_encrypted_state", return_value=status):
                with self.assertRaisesRegex(RuntimeError, "not durably persisted"):
                    persist_strict(repo=Path(tmp), encrypted=Path(tmp) / "x.enc", branch="agent-state", run_number="2", report=report)
            payload = json.loads(report.read_text(encoding="utf-8"))
            self.assertFalse(payload["pushed"])
            self.assertFalse(report.with_name(report.name + ".tmp").exists())


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import unittest
from pathlib import Path

from scripts.runtime_reliability import FailureClass


class ReliabilityFailureMatrixTests(unittest.TestCase):
    def test_matrix_covers_every_runtime_failure_class_once(self) -> None:
        path = Path(__file__).with_name("reliability_failure_matrix.json")
        data = json.loads(path.read_text(encoding="utf-8"))
        rows = data["classes"]
        ids = [row["id"] for row in rows]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(set(ids), {item.value for item in FailureClass})
        for row in rows:
            self.assertTrue(str(row["owner"]).strip())
            self.assertGreaterEqual(int(row["max_semantic_reasks"]), 0)
            self.assertLessEqual(int(row["max_semantic_reasks"]), 1)

    def test_global_reliability_invariants_are_fail_closed(self) -> None:
        path = Path(__file__).with_name("reliability_failure_matrix.json")
        principles = json.loads(path.read_text(encoding="utf-8"))["principles"]
        self.assertEqual(principles["retry_owner_count"], 1)
        self.assertFalse(principles["partial_apply_allowed"])
        self.assertFalse(principles["quality_gates_may_be_lowered_by_recovery"])
        self.assertEqual(principles["unknown_failure_policy"], "fail_closed")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.patch_production_workflow_for_preflight import replace_once


class WorkflowPatcherTests(unittest.TestCase):
    def test_replace_once_rejects_missing_marker(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "found 0"):
            replace_once("abc", "missing", "new")

    def test_replace_once_rejects_duplicate_marker(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "found 2"):
            replace_once("x x", "x", "y")

    def test_replace_once_changes_exactly_one_marker(self) -> None:
        self.assertEqual(replace_once("a old b", "old", "new"), "a new b")


if __name__ == "__main__":
    unittest.main()

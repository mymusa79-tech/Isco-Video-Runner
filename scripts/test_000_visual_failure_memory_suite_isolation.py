from __future__ import annotations

"""Keep aggregate Runner unittest jobs independent from shared cross-test history.

The production workflows intentionally restore one durable ISCO_HISTORY_PATH. Aggregate
unit-test jobs must not reuse that mutable file across unrelated fixture ids, otherwise
one test can teach that a fake stock asset failed and a later test can skip the same id
before its audit runs. This module sorts first in the full Runner regression and removes
only the inherited suite path. Persistence-specific tests install their own temporary
state explicitly.
"""

import os
import unittest


os.environ.pop("ISCO_HISTORY_PATH", None)


class VisualFailureMemorySuiteIsolationTests(unittest.TestCase):
    def test_general_suite_does_not_inherit_shared_history_path(self) -> None:
        self.assertNotIn("ISCO_HISTORY_PATH", os.environ)


if __name__ == "__main__":
    unittest.main()

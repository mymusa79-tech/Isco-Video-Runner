from __future__ import annotations

"""Keep aggregate Runner unittest jobs independent from mutable production history.

The production workflows intentionally restore one durable ``ISCO_HISTORY_PATH``. Aggregate
unit-test jobs must not reuse that mutable failure memory across unrelated fixture ids, but
they also must not fall back to a checked-in ``state/history.json`` inside the checkout.

Use one temporary suite history outside the repository and disable Engine visual-failure
learning only while that exact suite path is active. Persistence-specific tests install their
own temporary history paths, so the real cross-run learning contract remains exercised.
"""

import os
from pathlib import Path
import tempfile
import unittest


_SUITE_HISTORY_DIR = Path(tempfile.mkdtemp(prefix="isco-runner-suite-history-"))
_SUITE_HISTORY_PATH = str(_SUITE_HISTORY_DIR / "history.json")
os.environ["ISCO_HISTORY_PATH"] = _SUITE_HISTORY_PATH

try:
    import isco_video_agent.visual_failure_memory as visual_failure_memory
except ImportError:  # Some lightweight Runner-only checks do not install Engine.
    visual_failure_memory = None
else:
    _ORIGINAL_PERSISTENT_STATE_CONFIGURED = visual_failure_memory._persistent_state_configured

    def _persistent_state_configured_for_tests() -> bool:
        configured = str(os.environ.get("ISCO_HISTORY_PATH") or "").strip()
        if configured == _SUITE_HISTORY_PATH:
            return False
        return _ORIGINAL_PERSISTENT_STATE_CONFIGURED()

    visual_failure_memory._persistent_state_configured = _persistent_state_configured_for_tests


class VisualFailureMemorySuiteIsolationTests(unittest.TestCase):
    def test_general_suite_uses_ephemeral_history(self) -> None:
        self.assertEqual(os.environ.get("ISCO_HISTORY_PATH"), _SUITE_HISTORY_PATH)
        self.assertFalse(Path(_SUITE_HISTORY_PATH).is_relative_to(Path.cwd()))
        if visual_failure_memory is not None:
            self.assertFalse(visual_failure_memory._persistent_state_configured())


if __name__ == "__main__":
    unittest.main()

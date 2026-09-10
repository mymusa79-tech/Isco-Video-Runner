from __future__ import annotations

import os
import sys


def _is_standalone_run92_unittest() -> bool:
    """True only for the Stage-Ladder style one-module unittest invocation.

    `test_run92_opening_feasibility_guard` uses deliberately tiny repeated stock ids
    across independent test cases. A production-like shared ISCO_HISTORY_PATH would let
    persistent visual-failure learning from one case alter the next case before Vision,
    making test order observable. Full aggregate suites are isolated by the sorted
    test_000 sentinel instead; production entrypoints do not match this argv shape.
    """
    targets = [arg for arg in sys.argv[1:] if arg.startswith("scripts.test_")]
    return targets == ["scripts.test_run92_opening_feasibility_guard"]


if _is_standalone_run92_unittest():
    os.environ.pop("ISCO_HISTORY_PATH", None)

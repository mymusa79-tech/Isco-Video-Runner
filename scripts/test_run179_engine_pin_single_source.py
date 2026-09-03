from __future__ import annotations

import unittest

from scripts.preproduction_contract import _canonical_engine_pin_issues


class Run179EnginePinSingleSourceTests(unittest.TestCase):
    def test_matching_live_bindings_accept_any_exact_sha_without_python_copy(self) -> None:
        sha = "a" * 40
        workflow = f"""
env:
  EXPECTED_ENGINE_SHA: {sha}
steps:
  - uses: actions/checkout@example
    with:
      repository: mymusa79-tech/Isco-Video-Agent
      ref: {sha}
  - run: production
    env:
      ISCO_ENGINE_SHA: {sha}
"""
        self.assertEqual(_canonical_engine_pin_issues(workflow), [])

    def test_mismatched_runtime_pin_fails_closed(self) -> None:
        expected = "a" * 40
        wrong = "b" * 40
        workflow = f"""
env:
  EXPECTED_ENGINE_SHA: {expected}
steps:
  - uses: actions/checkout@example
    with:
      repository: mymusa79-tech/Isco-Video-Agent
      ref: {expected}
  - run: production
    env:
      ISCO_ENGINE_SHA: {wrong}
"""
        issues = _canonical_engine_pin_issues(workflow)
        self.assertEqual([item.code for item in issues], ["engine_pin_mismatch"])

    def test_missing_one_of_three_live_bindings_fails_closed(self) -> None:
        sha = "a" * 40
        workflow = f"""
env:
  EXPECTED_ENGINE_SHA: {sha}
steps:
  - uses: actions/checkout@example
    with:
      repository: mymusa79-tech/Isco-Video-Agent
      ref: {sha}
"""
        issues = _canonical_engine_pin_issues(workflow)
        self.assertEqual([item.code for item in issues], ["engine_pin"])


if __name__ == "__main__":
    unittest.main()

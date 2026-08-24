from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.workflow_hygiene import assert_workflow_hygiene, audit_workflows


class WorkflowHygieneTests(unittest.TestCase):
    def test_repository_workflows_are_hygienic(self) -> None:
        assert_workflow_hygiene(Path(__file__).resolve().parents[1])

    def test_run_number_workflow_is_rejected_even_if_otherwise_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / ".github" / "workflows"
            folder.mkdir(parents=True)
            (folder / "run999-debug.yml").write_text(
                "name: old\non: workflow_dispatch\npermissions:\n  contents: read\n"
                "jobs:\n  x:\n    runs-on: ubuntu-latest\n    steps:\n"
                "      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1\n",
                encoding="utf-8",
            )
            codes = {issue.code for issue in audit_workflows(root)}
            self.assertIn("run_specific_workflow_forbidden", codes)

    def test_unpinned_external_action_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / ".github" / "workflows"
            folder.mkdir(parents=True)
            (folder / "verify.yml").write_text(
                "name: verify\non: pull_request\npermissions:\n  contents: read\n"
                "jobs:\n  x:\n    runs-on: ubuntu-latest\n    steps:\n"
                "      - uses: actions/checkout@v4\n",
                encoding="utf-8",
            )
            codes = {issue.code for issue in audit_workflows(root)}
            self.assertIn("action_not_pinned_to_full_sha", codes)


if __name__ == "__main__":
    unittest.main()

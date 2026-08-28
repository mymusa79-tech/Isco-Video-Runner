from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.workflow_hygiene import assert_workflow_hygiene, audit_workflows


_PIN = "a" * 40
_OTHER_PIN = "b" * 40
_CHECKOUT = "3d3c42e5aac5ba805825da76410c181273ba90b1"


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
                f"      - uses: actions/checkout@{_CHECKOUT}\n",
                encoding="utf-8",
            )
            codes = {issue.code for issue in audit_workflows(root)}
            self.assertIn("run_specific_workflow_forbidden", codes)

    def test_retired_one_time_workflow_families_are_rejected(self) -> None:
        retired = (
            "migrate-memory-and-launch-production.yml",
            "youtube-analytics-backfill-write-once.yml",
            "youtube-analytics-backfill-write-once-v4.yml",
            "local-brain-smoke.yml",
            "p0c-migration-contracts.yml",
        )
        for name in retired:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                folder = root / ".github" / "workflows"
                folder.mkdir(parents=True)
                (folder / name).write_text(
                    "name: retired\non: workflow_dispatch\npermissions:\n  contents: read\n",
                    encoding="utf-8",
                )
                codes = {issue.code for issue in audit_workflows(root)}
                self.assertIn("retired_one_time_workflow_forbidden", codes)

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

    def test_live_workflow_engine_pin_drift_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / ".github" / "workflows"
            folder.mkdir(parents=True)
            (folder / "produce-resilient-v4.yml").write_text(
                "name: production\non: workflow_dispatch\npermissions:\n  contents: read\n"
                "jobs:\n  p:\n    runs-on: ubuntu-latest\n    steps:\n"
                f"      - uses: actions/checkout@{_CHECKOUT}\n"
                "        with:\n"
                "          repository: mymusa79-tech/Isco-Video-Agent\n"
                f"          ref: {_PIN}\n"
                "    env:\n"
                f"      ISCO_ENGINE_SHA: {_PIN}\n",
                encoding="utf-8",
            )
            (folder / "telegram-editorial-control.yml").write_text(
                "name: control\non: workflow_dispatch\npermissions:\n  contents: read\n"
                f"env:\n  ENGINE_SHA: {_OTHER_PIN}\n"
                "jobs:\n  x:\n    runs-on: ubuntu-latest\n    steps:\n"
                f"      - uses: actions/checkout@{_CHECKOUT}\n",
                encoding="utf-8",
            )
            issues = audit_workflows(root)
            drifts = [issue for issue in issues if issue.code == "engine_pin_drift"]
            self.assertEqual(len(drifts), 1)
            self.assertIn("telegram-editorial-control.yml", drifts[0].path)


if __name__ == "__main__":
    unittest.main()

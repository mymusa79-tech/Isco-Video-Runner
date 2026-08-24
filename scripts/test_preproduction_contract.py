from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.preproduction_contract import audit_preproduction_contract


class PreProductionContractTests(unittest.TestCase):
    def _repo(self, workflow: str) -> tuple[tempfile.TemporaryDirectory, Path]:
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        target = root / ".github" / "workflows" / "produce-resilient-v4.yml"
        target.parent.mkdir(parents=True)
        target.write_text(workflow, encoding="utf-8")
        return tmp, root

    def test_current_repository_contract_is_clean(self) -> None:
        self.assertEqual(audit_preproduction_contract(Path(".")), [])

    def test_moving_runner_alias_is_rejected(self) -> None:
        tmp, root = self._repo("on:\n  workflow_dispatch:\nconcurrency:\njobs:\n  x:\n    runs-on: ubuntu-latest\n")
        try:
            codes = {item.code for item in audit_preproduction_contract(root)}
        finally:
            tmp.cleanup()
        self.assertIn("runner_image", codes)

    def test_unreviewed_automatic_trigger_is_rejected(self) -> None:
        tmp, root = self._repo("on:\n  workflow_dispatch:\n  push:\nconcurrency:\njobs:\n  x:\n    runs-on: ubuntu-24.04\n")
        try:
            codes = {item.code for item in audit_preproduction_contract(root)}
        finally:
            tmp.cleanup()
        self.assertIn("production_trigger", codes)

    def test_release_namespace_guard_is_required(self) -> None:
        tmp, root = self._repo("on:\n  workflow_dispatch:\nconcurrency:\njobs:\n  x:\n    runs-on: ubuntu-24.04\n")
        try:
            codes = {item.code for item in audit_preproduction_contract(root)}
        finally:
            tmp.cleanup()
        self.assertIn("release_namespace_guard", codes)


if __name__ == "__main__":
    unittest.main()

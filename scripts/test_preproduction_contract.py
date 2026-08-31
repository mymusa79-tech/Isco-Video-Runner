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

    def test_release_namespace_guard_may_live_in_wrapped_core(self) -> None:
        tmp, root = self._repo("on:\n  workflow_dispatch:\nconcurrency:\njobs:\n  x:\n    runs-on: ubuntu-24.04\n")
        try:
            scripts = root / "scripts"
            scripts.mkdir(parents=True)
            (scripts / "environment_preflight.py").write_text(
                "from scripts import environment_preflight_core as _core\n",
                encoding="utf-8",
            )
            (scripts / "environment_preflight_core.py").write_text(
                "def _release_namespace_status(repository, release_tag):\n    return 'available'\n",
                encoding="utf-8",
            )
            codes = {item.code for item in audit_preproduction_contract(root)}
        finally:
            tmp.cleanup()
        self.assertNotIn("release_namespace_guard", codes)

    def test_release_collision_reconciliation_requires_exact_target_receipt_media_and_draft_guards(self) -> None:
        tmp, root = self._repo("on:\n  workflow_dispatch:\nconcurrency:\njobs:\n  x:\n    runs-on: ubuntu-24.04\n")
        try:
            scripts = root / "scripts"
            scripts.mkdir(parents=True)
            (scripts / "environment_preflight.py").write_text(
                "from scripts import environment_preflight_core as _core\n",
                encoding="utf-8",
            )
            (scripts / "environment_preflight_core.py").write_text(
                "def _release_namespace_status(repository, release_tag):\n"
                "    raise RuntimeError('existing release tag belongs to a different Runner SHA')\n",
                encoding="utf-8",
            )
            (scripts / "release_transaction.py").write_text(
                "def publish():\n"
                "    _assert_release_identity(existing, tag=tag, target_sha=target_sha)\n"
                "    _download_and_verify_receipt(existing)\n"
                "    # media-byte and exact-draft guards are intentionally absent\n",
                encoding="utf-8",
            )
            codes = {item.code for item in audit_preproduction_contract(root)}
        finally:
            tmp.cleanup()
        self.assertIn("release_collision_fail_closed", codes)
        self.assertNotIn("release_collision_target_guard", codes)

    def test_release_target_sha_binding_is_required(self) -> None:
        tmp, root = self._repo("on:\n  workflow_dispatch:\nconcurrency:\njobs:\n  x:\n    runs-on: ubuntu-24.04\n")
        try:
            codes = {item.code for item in audit_preproduction_contract(root)}
        finally:
            tmp.cleanup()
        self.assertIn("release_target_binding", codes)

    def test_orphan_tag_guard_may_live_in_wrapped_core(self) -> None:
        tmp, root = self._repo("on:\n  workflow_dispatch:\nconcurrency:\njobs:\n  x:\n    runs-on: ubuntu-24.04\n")
        try:
            scripts = root / "scripts"
            scripts.mkdir(parents=True)
            (scripts / "environment_preflight.py").write_text(
                "from scripts import environment_preflight_core as _core\n",
                encoding="utf-8",
            )
            (scripts / "environment_preflight_core.py").write_text(
                'ref_url = f"https://api.github.com/repos/{repository}/git/ref/tags/{encoded_tag}"\n',
                encoding="utf-8",
            )
            codes = {item.code for item in audit_preproduction_contract(root)}
        finally:
            tmp.cleanup()
        self.assertNotIn("release_orphan_tag_guard", codes)

    def test_orphan_tag_guard_still_fails_when_missing_from_wrapper_and_core(self) -> None:
        tmp, root = self._repo("on:\n  workflow_dispatch:\nconcurrency:\njobs:\n  x:\n    runs-on: ubuntu-24.04\n")
        try:
            scripts = root / "scripts"
            scripts.mkdir(parents=True)
            (scripts / "environment_preflight.py").write_text(
                "from scripts import environment_preflight_core as _core\n",
                encoding="utf-8",
            )
            (scripts / "environment_preflight_core.py").write_text(
                "def main():\n    pass\n",
                encoding="utf-8",
            )
            codes = {item.code for item in audit_preproduction_contract(root)}
        finally:
            tmp.cleanup()
        self.assertIn("release_orphan_tag_guard", codes)


if __name__ == "__main__":
    unittest.main()

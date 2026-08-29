from __future__ import annotations

import hashlib
import unittest
from pathlib import Path


class ReleaseReconciliationContractTests(unittest.TestCase):
    @staticmethod
    def _git_blob_sha(path: str) -> str:
        payload = Path(path).read_bytes()
        return hashlib.sha1(f"blob {len(payload)}\0".encode("ascii") + payload).hexdigest()

    def test_render_and_release_authority_layers_remain_byte_identical(self) -> None:
        self.assertEqual(
            self._git_blob_sha("scripts/render_durable_cache.py"),
            "abc92b472373cada7b92a7a53007ae943de98b27",
        )
        self.assertEqual(
            self._git_blob_sha("scripts/runtime_reliability.py"),
            "e38e88df66aa224c6968ff74730a59ad4c060343",
        )

    def test_reconciliation_never_overwrites_a_published_release(self) -> None:
        source = Path("scripts/release_transaction.py").read_text(encoding="utf-8")
        self.assertIn("reconciled_existing_published", source)
        self.assertIn("existing_published_conflict", source)
        self.assertIn("remote != expected", source)
        self.assertNotIn("--clobber", source)
        self.assertNotIn("release edit", source.replace('"gh", "release", "edit"', ""))

    def test_draft_rollback_cleans_the_git_tag(self) -> None:
        source = Path("scripts/release_transaction.py").read_text(encoding="utf-8")
        self.assertIn("--cleanup-tag", source)
        self.assertIn("_remove_exact_existing_draft", source)
        self.assertIn("target_sha=target_sha", source)

    def test_preflight_only_reconciles_an_exact_current_sha_release(self) -> None:
        source = Path("scripts/environment_preflight_core.py").read_text(encoding="utf-8")
        self.assertIn("target_commitish", source)
        self.assertIn("reconcile_existing_published", source)
        self.assertIn("reconcile_existing_draft", source)
        self.assertIn("existing orphan Git tag blocks", source)
        self.assertIn('target_sha = (os.environ.get("GITHUB_SHA")', source)

    def test_publish_approval_still_precedes_github_release(self) -> None:
        workflow = Path(".github/workflows/produce-resilient-v4.yml").read_text(encoding="utf-8")
        approval = workflow.index("      - name: Request publish approval via Telegram")
        release = workflow.index("      - name: Create GitHub Release with unified delivery bundle")
        self.assertLess(approval, release)
        self.assertIn("steps.publish_approval.outputs.effective_decision == 'approved'", workflow)


if __name__ == "__main__":
    unittest.main()

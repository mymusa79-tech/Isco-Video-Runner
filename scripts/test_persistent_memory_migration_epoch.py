from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import persistent_memory as pm
from scripts import persistent_memory_crypto as crypto


PRODUCTION_WORKFLOW_REF = (
    "mymusa79-tech/Isco-Video-Runner/.github/workflows/produce-resilient-v4.yml@refs/heads/main"
)


class PersistentMemoryMigrationEpochTests(unittest.TestCase):
    def _legacy_payload(self, root: Path) -> Path:
        path = root / "history.json.enc"
        path.write_bytes(b"Salted__" + b"x" * 64)
        return path

    def test_canonical_run_111_can_use_the_pinned_legacy_migration_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            encrypted = self._legacy_payload(root)
            out = root / "history.json"
            pin = "a" * 40
            env = {
                "GITHUB_WORKFLOW_REF": PRODUCTION_WORKFLOW_REF,
                "GITHUB_RUN_NUMBER": "111",
            }
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(pm, "APPROVED_LEGACY_BLOB_SHAS", frozenset({pin})),
                patch.object(pm, "_legacy_decrypt", return_value=b'{"videos":[]}\n') as decrypt,
            ):
                status = pm.decrypt_history(
                    encrypted,
                    out,
                    "key",
                    legacy_blob_sha=pin,
                )
            self.assertTrue(status.save_allowed)
            self.assertEqual(status.source, "encrypted-legacy-migration")
            decrypt.assert_called_once()
            self.assertEqual(json.loads(out.read_text(encoding="utf-8")), {"videos": []})

    def test_canonical_run_112_rejects_legacy_before_openssl_decrypt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            encrypted = self._legacy_payload(root)
            out = root / "history.json"
            pin = "a" * 40
            env = {
                "GITHUB_WORKFLOW_REF": PRODUCTION_WORKFLOW_REF,
                "GITHUB_RUN_NUMBER": "112",
            }
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(pm, "APPROVED_LEGACY_BLOB_SHAS", frozenset({pin})),
                patch.object(pm, "_legacy_decrypt") as decrypt,
            ):
                status = pm.decrypt_history(
                    encrypted,
                    out,
                    "key",
                    legacy_blob_sha=pin,
                )
            self.assertFalse(status.save_allowed)
            self.assertIn("authorized only for canonical production run 111", status.reason)
            decrypt.assert_not_called()
            self.assertEqual(json.loads(out.read_text(encoding="utf-8")), {"videos": []})

    def test_verifier_workflows_are_not_mistaken_for_production_epoch(self) -> None:
        payload = b"Salted__" + b"x" * 64
        env = {
            "GITHUB_WORKFLOW_REF": (
                "mymusa79-tech/Isco-Video-Runner/.github/workflows/verify-private-engine.yml@refs/pull/1/merge"
            ),
            "GITHUB_RUN_NUMBER": "999",
        }
        with patch.dict(os.environ, env, clear=False):
            self.assertFalse(crypto.is_authenticated_v2(payload))

    def test_authenticated_v2_remains_valid_after_run_111(self) -> None:
        metadata = crypto.metadata_from_values(
            run_number="111",
            previous_state_commit="none",
        )
        payload = crypto.seal(b'{"videos":[]}\n', "key", metadata=metadata)
        env = {
            "GITHUB_WORKFLOW_REF": PRODUCTION_WORKFLOW_REF,
            "GITHUB_RUN_NUMBER": "112",
        }
        with patch.dict(os.environ, env, clear=False):
            self.assertTrue(crypto.is_authenticated_v2(payload))
            plaintext, restored = crypto.open_envelope(payload, "key")
        self.assertEqual(plaintext, b'{"videos":[]}\n')
        self.assertEqual(restored.sequence, 111)

    def test_general_legacy_contracts_are_hermetic_inside_canonical_run_112(self) -> None:
        env = dict(os.environ)
        env.update({
            "GITHUB_WORKFLOW_REF": PRODUCTION_WORKFLOW_REF,
            "GITHUB_RUN_NUMBER": "112",
        })
        selected = [
            "scripts.test_persistent_memory.PersistentMemoryTests.test_arbitrary_legacy_cbc_is_rejected",
            "scripts.test_persistent_memory.PersistentMemoryTests.test_pinned_legacy_cbc_can_migrate_once",
            (
                "scripts.test_persistent_memory_regression_preservation."
                "PersistentMemoryRegressionPreservationTests."
                "test_unapproved_remote_legacy_blob_locks_save_before_decrypt"
            ),
        ]
        result = subprocess.run(
            [sys.executable, "-m", "unittest", *selected, "-q"],
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()

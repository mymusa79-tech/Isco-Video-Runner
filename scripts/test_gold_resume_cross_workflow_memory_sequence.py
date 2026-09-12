from __future__ import annotations

import json
import os
import runpy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.persistent_memory_crypto import metadata_from_values, seal
from scripts.state_persistence_strict import _effective_run_number


class GoldResumeCrossWorkflowMemorySequenceTests(unittest.TestCase):
    def test_resume_encrypt_advances_from_authenticated_restore_identity(self) -> None:
        namespace = runpy.run_path("scripts/persistent_memory.py", run_name="persistent_memory_wrapper_test")
        next_sequence = namespace["_gold_resume_next_state_sequence"]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            accepted = root / "history.accepted.json"
            accepted.write_text('{"videos": []}\n', encoding="utf-8")
            (root / ".persistent-memory-identity.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "save_allowed": True,
                        "source": "agent-state",
                        "state_commit": "a" * 40,
                        "state_sequence": 245,
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"GITHUB_RUN_NUMBER": "1"}, clear=False):
                self.assertEqual(next_sequence(accepted), "246")

    def test_resume_persistence_uses_authenticated_envelope_sequence_not_workflow_local_counter(self) -> None:
        key = "gold-resume-sequence-test-key"
        metadata = metadata_from_values(run_number="246", previous_state_commit="a" * 40)
        payload = seal(b'{"videos":[]}\n', key, metadata=metadata)
        with tempfile.TemporaryDirectory() as td:
            encrypted = Path(td) / "history.enc"
            encrypted.write_bytes(payload)
            with mock.patch.dict(
                os.environ,
                {
                    "GITHUB_WORKFLOW": "Resume Gold QC Pending",
                    "STATE_ENCRYPTION_KEY": key,
                },
                clear=False,
            ):
                self.assertEqual(_effective_run_number(encrypted, "1"), "246")

    def test_canonical_workflows_keep_exact_requested_run_number(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"GITHUB_WORKFLOW": "Production V4", "STATE_ENCRYPTION_KEY": "unused"},
            clear=False,
        ):
            self.assertEqual(_effective_run_number(Path("does-not-need-to-exist"), "246"), "246")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import os
import runpy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scripts.gold_resume_workflow_identity import gold_resume_workflow_identity
from scripts.persistent_memory_crypto import metadata_from_values, seal
from scripts.state_persistence_strict import _effective_run_number


class GoldResumeCrossWorkflowMemorySequenceTests(unittest.TestCase):
    @staticmethod
    def _gold_resume_env(**extra: str) -> dict[str, str]:
        env = {
            "GITHUB_ACTIONS": "true",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_WORKFLOW": "Resume Gold QC Pending",
            "GITHUB_WORKFLOW_REF": (
                "mymusa79-tech/Isco-Video-Runner/.github/workflows/"
                "resume-gold-qc-pending.yml@refs/heads/main"
            ),
        }
        env.update(extra)
        return env

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

    def test_resume_sequence_ignores_unrelated_high_workflow_local_counter(self) -> None:
        namespace = runpy.run_path("scripts/persistent_memory.py", run_name="persistent_memory_wrapper_high_counter_test")
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
                        "state_commit": "b" * 40,
                        "state_sequence": 245,
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"GITHUB_RUN_NUMBER": "999"}, clear=False):
                self.assertEqual(next_sequence(accepted), "246")

    def test_exact_workflow_ref_authenticates_gold_resume_exception(self) -> None:
        with mock.patch.dict(os.environ, self._gold_resume_env(), clear=True):
            self.assertTrue(gold_resume_workflow_identity())

    def test_same_display_name_wrong_workflow_ref_cannot_authenticate(self) -> None:
        env = self._gold_resume_env()
        env["GITHUB_WORKFLOW_REF"] = (
            "mymusa79-tech/Isco-Video-Runner/.github/workflows/other.yml@refs/heads/main"
        )
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertFalse(gold_resume_workflow_identity())

    def test_display_name_alone_cannot_activate_sequence_exception(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"GITHUB_WORKFLOW": "Resume Gold QC Pending"},
            clear=True,
        ):
            self.assertFalse(gold_resume_workflow_identity())
            self.assertEqual(
                _effective_run_number(Path("does-not-need-to-exist"), "7"),
                "7",
            )

    def test_authenticated_gold_resume_restore_suppresses_only_workflow_local_counter(self) -> None:
        namespace = runpy.run_path(
            "scripts/persistent_memory.py",
            run_name="persistent_memory_authenticated_restore_test",
        )
        seen_run_numbers: list[str | None] = []
        args = SimpleNamespace(
            command="restore",
            repo=".",
            func=lambda _args: seen_run_numbers.append(os.environ.get("GITHUB_RUN_NUMBER")) or 0,
        )
        parser = mock.Mock()
        parser.parse_args.return_value = args
        with (
            mock.patch.dict(
                os.environ,
                self._gold_resume_env(GITHUB_RUN_NUMBER="1"),
                clear=True,
            ),
            mock.patch.object(namespace["_core"], "build_parser", return_value=parser),
        ):
            self.assertEqual(namespace["main"]([]), 0)
            self.assertEqual(seen_run_numbers, [None])
            self.assertEqual(os.environ.get("GITHUB_RUN_NUMBER"), "1")

    def test_spoofed_display_name_does_not_suppress_restore_counter(self) -> None:
        namespace = runpy.run_path(
            "scripts/persistent_memory.py",
            run_name="persistent_memory_spoofed_restore_test",
        )
        seen_run_numbers: list[str | None] = []
        args = SimpleNamespace(
            command="restore",
            repo=".",
            func=lambda _args: seen_run_numbers.append(os.environ.get("GITHUB_RUN_NUMBER")) or 0,
        )
        parser = mock.Mock()
        parser.parse_args.return_value = args
        env = self._gold_resume_env(GITHUB_RUN_NUMBER="1")
        env["GITHUB_WORKFLOW_REF"] = (
            "mymusa79-tech/Isco-Video-Runner/.github/workflows/other.yml@refs/heads/main"
        )
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(namespace["_core"], "build_parser", return_value=parser),
        ):
            self.assertEqual(namespace["main"]([]), 0)
            self.assertEqual(seen_run_numbers, ["1"])
            self.assertEqual(os.environ.get("GITHUB_RUN_NUMBER"), "1")

    def test_resume_persistence_uses_authenticated_envelope_sequence_not_workflow_local_counter(self) -> None:
        key = "gold-resume-sequence-test-key"
        metadata = metadata_from_values(run_number="246", previous_state_commit="a" * 40)
        payload = seal(b'{"videos":[]}\n', key, metadata=metadata)
        with tempfile.TemporaryDirectory() as td:
            encrypted = Path(td) / "history.enc"
            encrypted.write_bytes(payload)
            with mock.patch.dict(
                os.environ,
                self._gold_resume_env(STATE_ENCRYPTION_KEY=key),
                clear=True,
            ):
                self.assertEqual(_effective_run_number(encrypted, "1"), "246")

    def test_non_resume_workflow_keeps_exact_requested_run_number_even_with_same_display_name(self) -> None:
        env = self._gold_resume_env(STATE_ENCRYPTION_KEY="unused")
        env["GITHUB_WORKFLOW_REF"] = (
            "mymusa79-tech/Isco-Video-Runner/.github/workflows/"
            "produce-resilient-v4.yml@refs/heads/main"
        )
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(
                _effective_run_number(Path("does-not-need-to-exist"), "246"),
                "246",
            )


if __name__ == "__main__":
    unittest.main()

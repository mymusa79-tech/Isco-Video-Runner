from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts import persistent_memory as pm
from scripts.gold_resume_workflow_identity import gold_resume_workflow_identity
from scripts.persistent_memory import PersistStatus
from scripts.persistent_memory_crypto import metadata_from_values, seal
from scripts.state_persistence_strict import _effective_run_number, persist_strict


class StatePersistenceStrictTests(unittest.TestCase):
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

    def test_successful_or_unchanged_push_closes_state(self) -> None:
        for status in (
            PersistStatus(True, True, "updated"),
            PersistStatus(True, False, "unchanged"),
        ):
            with self.subTest(status=status):
                with tempfile.TemporaryDirectory() as tmp:
                    report = Path(tmp) / "state.json"
                    with (
                        patch.dict(os.environ, {}, clear=True),
                        patch("scripts.state_persistence_strict.persist_encrypted_state", return_value=status),
                    ):
                        persist_strict(
                            repo=Path(tmp),
                            encrypted=Path(tmp) / "x.enc",
                            branch="agent-state",
                            run_number="1",
                            report=report,
                        )
                    self.assertTrue(json.loads(report.read_text(encoding="utf-8"))["pushed"])

    def test_failed_state_push_is_a_hard_incomplete_closure(self) -> None:
        status = PersistStatus(False, True, "push rejected")
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "state.json"
            with (
                patch.dict(os.environ, {}, clear=True),
                patch("scripts.state_persistence_strict.persist_encrypted_state", return_value=status),
            ):
                with self.assertRaisesRegex(RuntimeError, "not durably persisted"):
                    persist_strict(
                        repo=Path(tmp),
                        encrypted=Path(tmp) / "x.enc",
                        branch="agent-state",
                        run_number="2",
                        report=report,
                    )
            payload = json.loads(report.read_text(encoding="utf-8"))
            self.assertFalse(payload["pushed"])
            self.assertFalse(report.with_name(report.name + ".tmp").exists())

    def test_exact_workflow_ref_authenticates_gold_resume_exception(self) -> None:
        with patch.dict(os.environ, self._gold_resume_env(), clear=True):
            self.assertTrue(gold_resume_workflow_identity())

    def test_display_name_alone_cannot_activate_gold_resume_exception(self) -> None:
        with patch.dict(
            os.environ,
            {"GITHUB_WORKFLOW": "Resume Gold QC Pending"},
            clear=True,
        ):
            self.assertFalse(gold_resume_workflow_identity())
            self.assertEqual(
                _effective_run_number(Path("does-not-need-to-exist"), "7"),
                "7",
            )

    def test_same_display_name_wrong_workflow_ref_cannot_activate_exception(self) -> None:
        env = self._gold_resume_env()
        env["GITHUB_WORKFLOW_REF"] = (
            "mymusa79-tech/Isco-Video-Runner/.github/workflows/other.yml@refs/heads/main"
        )
        with patch.dict(os.environ, env, clear=True):
            self.assertFalse(gold_resume_workflow_identity())
            self.assertEqual(
                _effective_run_number(Path("does-not-need-to-exist"), "7"),
                "7",
            )

    def test_gold_resume_persistence_uses_authenticated_envelope_sequence(self) -> None:
        key = "gold-resume-sequence-test-key"
        metadata = metadata_from_values(run_number="246", previous_state_commit="a" * 40)
        payload = seal(b'{"videos":[]}\n', key, metadata=metadata)
        with tempfile.TemporaryDirectory() as tmp:
            encrypted = Path(tmp) / "history.enc"
            encrypted.write_bytes(payload)
            with patch.dict(
                os.environ,
                self._gold_resume_env(STATE_ENCRYPTION_KEY=key),
                clear=True,
            ):
                self.assertEqual(_effective_run_number(encrypted, "1"), "246")

    def test_authenticated_gold_resume_restore_suppresses_only_local_run_counter(self) -> None:
        seen_run_numbers: list[str | None] = []
        args = SimpleNamespace(
            command="restore",
            repo=".",
            func=lambda _args: seen_run_numbers.append(os.environ.get("GITHUB_RUN_NUMBER")) or 0,
        )
        parser = Mock()
        parser.parse_args.return_value = args
        with (
            patch.dict(
                os.environ,
                self._gold_resume_env(GITHUB_RUN_NUMBER="1"),
                clear=True,
            ),
            patch.object(pm, "build_parser", return_value=parser),
        ):
            self.assertEqual(pm.main([]), 0)
            self.assertEqual(seen_run_numbers, [None])
            self.assertEqual(os.environ.get("GITHUB_RUN_NUMBER"), "1")

    def test_spoofed_display_name_does_not_suppress_restore_counter(self) -> None:
        seen_run_numbers: list[str | None] = []
        args = SimpleNamespace(
            command="restore",
            repo=".",
            func=lambda _args: seen_run_numbers.append(os.environ.get("GITHUB_RUN_NUMBER")) or 0,
        )
        parser = Mock()
        parser.parse_args.return_value = args
        env = self._gold_resume_env(GITHUB_RUN_NUMBER="1")
        env["GITHUB_WORKFLOW_REF"] = (
            "mymusa79-tech/Isco-Video-Runner/.github/workflows/other.yml@refs/heads/main"
        )
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(pm, "build_parser", return_value=parser),
        ):
            self.assertEqual(pm.main([]), 0)
            self.assertEqual(seen_run_numbers, ["1"])
            self.assertEqual(os.environ.get("GITHUB_RUN_NUMBER"), "1")

    def test_gold_resume_encrypt_advances_authenticated_restore_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            accepted = root / "history.accepted.json"
            accepted.write_text('{"videos": []}\n', encoding="utf-8")
            pm.write_restore_identity(
                accepted,
                pm.RestoreStatus(
                    True,
                    "agent-state",
                    state_commit="a" * 40,
                    state_sequence=245,
                ),
            )
            seen_sequences: list[str | None] = []
            args = SimpleNamespace(
                command="encrypt",
                plain=str(accepted),
                run_number=None,
                func=lambda parsed: seen_sequences.append(parsed.run_number) or 0,
            )
            parser = Mock()
            parser.parse_args.return_value = args
            with (
                patch.dict(
                    os.environ,
                    self._gold_resume_env(GITHUB_RUN_NUMBER="1"),
                    clear=True,
                ),
                patch.object(pm, "build_parser", return_value=parser),
            ):
                self.assertEqual(pm.main([]), 0)
            self.assertEqual(seen_sequences, ["246"])


if __name__ == "__main__":
    unittest.main()

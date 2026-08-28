from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import immutable_planning_snapshot as snapshot
from scripts import planning_checkpoint_state as checkpoint


class Run129HermeticApprovedBriefTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_installed = snapshot._INSTALLED
        self.original_build_runtime_binding = checkpoint.build_runtime_binding

    def tearDown(self) -> None:
        snapshot._INSTALLED = self.original_installed
        checkpoint.build_runtime_binding = self.original_build_runtime_binding

    @staticmethod
    def _git(cwd: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return result.stdout.strip()

    def _engine_repo(self, root: Path, brief_bytes: bytes) -> tuple[Path, str]:
        engine = root / "engine"
        engine.mkdir()
        self._git(engine, "init")
        self._git(engine, "config", "user.name", "test")
        self._git(engine, "config", "user.email", "test@example.com")
        brief = engine / "production" / "approved_brief.json"
        brief.parent.mkdir(parents=True)
        brief.write_bytes(brief_bytes)
        (engine / "README.md").write_text("engine\n", encoding="utf-8")
        self._git(engine, "add", ".")
        self._git(engine, "commit", "-m", "engine")
        return engine, self._git(engine, "rev-parse", "HEAD")

    def test_snapshot_comes_from_engine_head_when_worktree_was_polluted(self) -> None:
        committed = (
            b'{"approved_by_user":true,"approved_topic":"approved","format":"film",'
            b'"research_pack":[{"source_title":"a","source_url":"https://a",'
            b'"claim_scope":"a"},{"source_title":"b","source_url":"https://b",'
            b'"claim_scope":"b"}]}\n'
        )
        polluted = b'{"approved_by_user":true,"approved_topic":"TEST_LEAK","format":"film"}\n'

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runner_temp = root / "runner-temp"
            runner_temp.mkdir()
            engine, engine_sha = self._engine_repo(root, committed)
            mutable = engine / "production" / "approved_brief.json"
            mutable.write_bytes(polluted)

            expected = hashlib.sha256(committed).hexdigest()
            env = {
                "RUNNER_TEMP": str(runner_temp),
                "ISCO_APPROVED_BRIEF_SHA256": expected,
                "ISCO_ENGINE_SHA": engine_sha,
            }
            with patch.dict(os.environ, env, clear=False), patch.object(
                checkpoint, "canonical_runtime_enabled", return_value=True
            ):
                path = snapshot.materialize_runtime_snapshot(root, engine)

            self.assertEqual(path.read_bytes(), committed)
            self.assertNotEqual(path.read_bytes(), mutable.read_bytes())
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), expected)
            self.assertEqual(path.stat().st_mode & 0o222, 0)

    def test_runtime_binding_overrides_historical_worktree_path(self) -> None:
        committed = (
            b'{"approved_by_user":true,"approved_topic":"approved","format":"film",'
            b'"research_pack":[{"source_title":"a","source_url":"https://a",'
            b'"claim_scope":"a"},{"source_title":"b","source_url":"https://b",'
            b'"claim_scope":"b"}]}\n'
        )

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runner_temp = root / "runner-temp"
            runner_temp.mkdir()
            engine, engine_sha = self._engine_repo(root, committed)
            expected = hashlib.sha256(committed).hexdigest()
            historical = engine / "production" / "approved_brief.json"

            env = {
                "RUNNER_TEMP": str(runner_temp),
                "ISCO_APPROVED_BRIEF_SHA256": expected,
                "ISCO_ENGINE_SHA": engine_sha,
                "ISCO_APPROVED_BRIEF_PATH": str(historical),
            }
            with patch.dict(os.environ, env, clear=False), patch.object(
                checkpoint, "canonical_runtime_enabled", return_value=True
            ):
                path = snapshot.materialize_runtime_snapshot(root, engine)
                snapshot._INSTALLED = False
                snapshot.install_runtime_snapshot_binding(force=True)
                self.assertEqual(Path(os.environ["ISCO_APPROVED_BRIEF_PATH"]).resolve(), path.resolve())
                binding = checkpoint.build_runtime_binding(root, engine)

            self.assertEqual(binding.approved_brief_sha256, expected)


if __name__ == "__main__":
    unittest.main()

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


RUNNER_ROOT = Path(__file__).resolve().parents[1]
_CANONICAL_WORKFLOW_REF = (
    "mymusa79-tech/Isco-Video-Runner/.github/workflows/"
    "produce-resilient-v4.yml@refs/heads/main"
)


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

    @staticmethod
    def _runtime_env(*, runner_temp: Path, approval_hash: str, engine_sha: str) -> dict[str, str]:
        return {
            "RUNNER_TEMP": str(runner_temp),
            # This is the Engine's canonical semantic approval fingerprint, not a raw
            # file-byte digest. Tests intentionally keep it independent of snapshot SHA.
            "ISCO_APPROVED_BRIEF_SHA256": approval_hash,
            "ISCO_APPROVED_BRIEF_SNAPSHOT_PATH": "",
            "ISCO_APPROVED_BRIEF_SNAPSHOT_SHA256": "",
            "ISCO_ENGINE_SHA": engine_sha,
            "GITHUB_ACTIONS": "true",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_WORKFLOW_REF": _CANONICAL_WORKFLOW_REF,
            "ISCO_CANONICAL_RUNTIME": "1",
        }

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

            expected_raw = hashlib.sha256(committed).hexdigest()
            approval_hash = "a" * 64
            self.assertNotEqual(approval_hash, expected_raw)
            env = self._runtime_env(
                runner_temp=runner_temp,
                approval_hash=approval_hash,
                engine_sha=engine_sha,
            )
            with patch.dict(os.environ, env, clear=False):
                path = snapshot.materialize_runtime_snapshot(RUNNER_ROOT, engine)
                self.assertEqual(os.environ["ISCO_APPROVED_BRIEF_SHA256"], approval_hash)
                self.assertEqual(
                    os.environ["ISCO_APPROVED_BRIEF_SNAPSHOT_SHA256"],
                    expected_raw,
                )

            self.assertEqual(path.read_bytes(), committed)
            self.assertNotEqual(path.read_bytes(), mutable.read_bytes())
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), expected_raw)
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
            expected_raw = hashlib.sha256(committed).hexdigest()
            approval_hash = "b" * 64
            self.assertNotEqual(approval_hash, expected_raw)
            historical = engine / "production" / "approved_brief.json"

            env = self._runtime_env(
                runner_temp=runner_temp,
                approval_hash=approval_hash,
                engine_sha=engine_sha,
            )
            env["ISCO_APPROVED_BRIEF_PATH"] = str(historical)
            with patch.dict(os.environ, env, clear=False):
                path = snapshot.materialize_runtime_snapshot(RUNNER_ROOT, engine)
                snapshot._INSTALLED = False
                snapshot.install_runtime_snapshot_binding(force=True)
                self.assertEqual(Path(os.environ["ISCO_APPROVED_BRIEF_PATH"]).resolve(), path.resolve())
                self.assertEqual(os.environ["ISCO_APPROVED_BRIEF_SHA256"], approval_hash)
                self.assertEqual(
                    os.environ["ISCO_APPROVED_BRIEF_SNAPSHOT_SHA256"],
                    expected_raw,
                )
                binding = checkpoint.build_runtime_binding(RUNNER_ROOT, engine)

            self.assertEqual(binding.approved_brief_sha256, expected_raw)

    def test_existing_snapshot_rejects_raw_byte_drift_independent_of_approval_hash(self) -> None:
        committed = b'{"approved_by_user":true,"approved_topic":"approved","format":"film"}\n'

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runner_temp = root / "runner-temp"
            runner_temp.mkdir()
            engine, engine_sha = self._engine_repo(root, committed)
            env = self._runtime_env(
                runner_temp=runner_temp,
                approval_hash="c" * 64,
                engine_sha=engine_sha,
            )
            with patch.dict(os.environ, env, clear=False):
                path = snapshot.materialize_runtime_snapshot(RUNNER_ROOT, engine)
                path.chmod(0o644)
                path.write_bytes(b"tampered\n")
                path.chmod(0o444)
                with self.assertRaisesRegex(RuntimeError, "pinned Engine bytes"):
                    snapshot.materialize_runtime_snapshot(RUNNER_ROOT, engine)


if __name__ == "__main__":
    unittest.main()

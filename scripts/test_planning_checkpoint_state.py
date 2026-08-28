from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import planning_checkpoint_state as state


class DurablePlanningCheckpointTests(unittest.TestCase):
    KEY = "durable-planning-test-key"

    def _git(self, cwd: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return result.stdout.strip()

    def _remote_and_clone(self, root: Path) -> tuple[Path, Path]:
        remote = root / "remote.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, stdout=subprocess.PIPE)
        repo = root / "repo"
        subprocess.run(["git", "init", str(repo)], check=True, stdout=subprocess.PIPE)
        self._git(repo, "config", "user.name", "test")
        self._git(repo, "config", "user.email", "test@example.com")
        (repo / "README.md").write_text("main\n", encoding="utf-8")
        self._git(repo, "add", "README.md")
        self._git(repo, "commit", "-m", "main")
        self._git(repo, "branch", "-M", "main")
        self._git(repo, "remote", "add", "origin", str(remote))
        self._git(repo, "push", "-u", "origin", "main")
        return remote, repo

    def _clone(self, remote: Path, target: Path) -> Path:
        subprocess.run(
            ["git", "clone", "--branch", "main", str(remote), str(target)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._git(target, "config", "user.name", "test")
        self._git(target, "config", "user.email", "test@example.com")
        return target

    def _binding(self, suffix: str = "0") -> state.Binding:
        return state.Binding(
            approved_brief_sha256=("a" * 63) + suffix,
            engine_sha=("b" * 39) + suffix,
            planning_contract_sha256=("c" * 63) + suffix,
        )

    @staticmethod
    def _router_key(model: str, prompt: str) -> str:
        # Exact persisted key contract in task_level_planner_router.py.
        return hashlib.sha256((model + "\n" + prompt).encode("utf-8")).hexdigest()

    def test_run125_s7_of_8_failure_resumes_next_run_at_s7(self) -> None:
        """Run125 shape: S1-S6 saved, S7 fails, next run's first provider work is S7."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            remote, run125_repo = self._remote_and_clone(root)
            binding = self._binding("0")
            run125_plain = root / "run125" / "planning-checkpoint.json"
            run125_identity = root / "run125" / "identity.json"
            run125_encrypted = root / "run125" / "planning-checkpoint.json.enc"

            initial = state.restore_from_git(
                run125_repo,
                run125_plain,
                run125_identity,
                self.KEY,
                binding,
                current_run_number="125",
            )
            self.assertTrue(initial.persist_allowed)
            self.assertFalse(initial.resume_allowed)

            model = "gemini-2.5-flash"
            prompts = {f"S{i}": f"SCRIPT_DOCTOR_SECTION_{i}_OF_8" for i in range(1, 9)}
            checkpoint = {
                "version": 1,
                "responses": {
                    self._router_key(model, prompts[f"S{i}"]): {
                        "sections": [
                            {"id": f"S{i}", "narration": f"done-{i}", "key_point": f"kp-{i}"}
                        ]
                    }
                    for i in range(1, 7)
                },
                "last_provider": "groq",
            }
            run125_plain.parent.mkdir(parents=True, exist_ok=True)
            run125_plain.write_text(json.dumps(checkpoint), encoding="utf-8")

            persisted = state.persist_checkpoint(
                repo_dir=run125_repo,
                plain_path=run125_plain,
                identity_path=run125_identity,
                encrypted_path=run125_encrypted,
                key=self.KEY,
                binding=binding,
                branch=state.STATE_BRANCH,
                run_number="125",
                status="in_progress",
            )
            self.assertTrue(persisted.pushed)
            self.assertTrue(persisted.changed)

            run126_repo = self._clone(remote, root / "run126-repo")
            run126_plain = root / "run126" / "planning-checkpoint.json"
            run126_identity = root / "run126" / "identity.json"
            restored = state.restore_from_git(
                run126_repo,
                run126_plain,
                run126_identity,
                self.KEY,
                binding,
                current_run_number="126",
            )
            self.assertTrue(restored.persist_allowed)
            self.assertTrue(restored.resume_allowed)

            loaded = json.loads(run126_plain.read_text(encoding="utf-8"))
            provider_sections: list[str] = []
            for section_id in [f"S{i}" for i in range(1, 9)]:
                key = self._router_key(model, prompts[section_id])
                if loaded["responses"].get(key) is None:
                    provider_sections.append(section_id)
                    loaded["responses"][key] = {
                        "sections": [{"id": section_id, "narration": "new", "key_point": "new"}]
                    }

            self.assertEqual(provider_sections, ["S7", "S8"])
            self.assertEqual(provider_sections[0], "S7")

    def test_authenticated_checkpoint_never_resumes_across_binding_change(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            remote, first_repo = self._remote_and_clone(root)
            old_binding = self._binding("0")
            plain = root / "first" / "checkpoint.json"
            identity = root / "first" / "identity.json"
            encrypted = root / "first" / "checkpoint.enc"
            state.restore_from_git(
                first_repo, plain, identity, self.KEY, old_binding, current_run_number="125"
            )
            plain.parent.mkdir(parents=True, exist_ok=True)
            plain.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "responses": {
                            hashlib.sha256(b"one").hexdigest(): {"sections": [{"id": "S1"}]}
                        },
                    }
                ),
                encoding="utf-8",
            )
            state.persist_checkpoint(
                repo_dir=first_repo,
                plain_path=plain,
                identity_path=identity,
                encrypted_path=encrypted,
                key=self.KEY,
                binding=old_binding,
                branch=state.STATE_BRANCH,
                run_number="125",
                status="in_progress",
            )

            variants = (
                (
                    "brief",
                    state.Binding("d" * 64, old_binding.engine_sha, old_binding.planning_contract_sha256),
                ),
                (
                    "engine",
                    state.Binding(
                        old_binding.approved_brief_sha256,
                        "e" * 40,
                        old_binding.planning_contract_sha256,
                    ),
                ),
                (
                    "contract",
                    state.Binding(old_binding.approved_brief_sha256, old_binding.engine_sha, "f" * 64),
                ),
            )
            for name, changed in variants:
                with self.subTest(name=name):
                    repo = self._clone(remote, root / f"repo-{name}")
                    out = root / name / "checkpoint.json"
                    ident = root / name / "identity.json"
                    restored = state.restore_from_git(
                        repo, out, ident, self.KEY, changed, current_run_number="126"
                    )
                    self.assertTrue(restored.persist_allowed)
                    self.assertFalse(restored.resume_allowed)
                    self.assertEqual(json.loads(out.read_text(encoding="utf-8")), state.EMPTY_CHECKPOINT)

    def test_completed_checkpoint_is_not_reused(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            remote, run125_repo = self._remote_and_clone(root)
            binding = self._binding("0")
            plain = root / "run125" / "checkpoint.json"
            identity = root / "run125" / "identity.json"
            encrypted = root / "run125" / "checkpoint.enc"
            state.restore_from_git(
                run125_repo, plain, identity, self.KEY, binding, current_run_number="125"
            )
            persisted = state.persist_checkpoint(
                repo_dir=run125_repo,
                plain_path=plain,
                identity_path=identity,
                encrypted_path=encrypted,
                key=self.KEY,
                binding=binding,
                branch=state.STATE_BRANCH,
                run_number="125",
                status="complete",
            )
            self.assertTrue(persisted.pushed)

            run126_repo = self._clone(remote, root / "run126")
            out = root / "next" / "checkpoint.json"
            ident = root / "next" / "identity.json"
            restored = state.restore_from_git(
                run126_repo, out, ident, self.KEY, binding, current_run_number="126"
            )
            self.assertTrue(restored.persist_allowed)
            self.assertFalse(restored.resume_allowed)
            self.assertIn("complete", restored.reason)

    def test_planning_contract_follows_transitive_local_imports_and_hashes_leaf_changes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            scripts = root / "scripts"
            scripts.mkdir(parents=True)
            (scripts / "run_v3_voice.py").write_text(
                "from scripts import layer_a\n",
                encoding="utf-8",
            )
            (scripts / "layer_a.py").write_text(
                "from .layer_b import install\n",
                encoding="utf-8",
            )
            (scripts / "layer_b.py").write_text(
                "def install():\n    return 'v1'\n",
                encoding="utf-8",
            )

            files = state.planning_contract_files(root)
            self.assertEqual(
                files,
                (
                    "scripts/layer_a.py",
                    "scripts/layer_b.py",
                    "scripts/run_v3_voice.py",
                ),
            )
            first = state.planning_contract_sha256(root)
            target = scripts / "layer_b.py"
            target.write_text("def install():\n    return 'v2'\n", encoding="utf-8")
            second = state.planning_contract_sha256(root)
            self.assertNotEqual(first, second)

    def test_dynamic_function_import_enters_contract_without_manual_registration(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            scripts = root / "scripts"
            scripts.mkdir(parents=True)
            (scripts / "run_v3_voice.py").write_text(
                "from scripts.bridge import install\n",
                encoding="utf-8",
            )
            (scripts / "bridge.py").write_text(
                "def install():\n    from .future_run127 import activate\n    return activate()\n",
                encoding="utf-8",
            )
            (scripts / "future_run127.py").write_text(
                "def activate():\n    return True\n",
                encoding="utf-8",
            )
            files = set(state.planning_contract_files(root))
            self.assertIn("scripts/future_run127.py", files)

    def test_current_live_closure_includes_run122_through_run125_layers(self) -> None:
        files = set(state.planning_contract_files(Path.cwd()))
        required = {
            "scripts/run_v3_voice.py",
            "scripts/runtime_closure.py",
            "scripts/run120_schema_policy_bridge.py",
            "scripts/run122_effective_capacity_admission.py",
            "scripts/run123_planning_latency_hardening.py",
            "scripts/run124_terminal_provider_recovery.py",
            "scripts/run125_capacity_routing_closure.py",
            "scripts/run125_cache_prefix_contract.py",
        }
        self.assertFalse(required - files, f"live planning/runtime contract missing: {sorted(required - files)}")

    def test_ciphertext_tamper_blocks_resume_and_future_persist(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            remote, repo = self._remote_and_clone(root)
            binding = self._binding("0")
            plain = root / "a" / "checkpoint.json"
            identity = root / "a" / "identity.json"
            encrypted = root / "a" / "checkpoint.enc"
            state.restore_from_git(repo, plain, identity, self.KEY, binding, current_run_number="125")
            plain.parent.mkdir(parents=True, exist_ok=True)
            plain.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "responses": {
                            hashlib.sha256(b"x").hexdigest(): {"sections": [{"id": "S1"}]}
                        },
                    }
                ),
                encoding="utf-8",
            )
            state.persist_checkpoint(
                repo_dir=repo,
                plain_path=plain,
                identity_path=identity,
                encrypted_path=encrypted,
                key=self.KEY,
                binding=binding,
                branch=state.STATE_BRANCH,
                run_number="125",
                status="in_progress",
            )

            self._git(repo, "checkout", state.STATE_BRANCH)
            target = repo / state.STATE_PATH
            payload = bytearray(target.read_bytes())
            payload[-8] ^= 0x01
            target.write_bytes(payload)
            self._git(repo, "add", state.STATE_PATH.as_posix())
            self._git(repo, "commit", "-m", "tamper")
            self._git(repo, "push", "origin", state.STATE_BRANCH)

            fresh = self._clone(remote, root / "fresh")
            out = root / "fresh-state" / "checkpoint.json"
            ident = root / "fresh-state" / "identity.json"
            restored = state.restore_from_git(
                fresh, out, ident, self.KEY, binding, current_run_number="126"
            )
            self.assertFalse(restored.persist_allowed)
            self.assertFalse(restored.resume_allowed)

    def test_runtime_wrapper_persists_failure_without_replacing_root_exception(self) -> None:
        root = RuntimeError("run125-s7-failure")
        calls: list[str] = []

        def produce():
            raise root

        orchestrator = SimpleNamespace(produce=produce)
        with patch.object(state, "canonical_runtime_enabled", return_value=True), patch.object(
            state, "persist_runtime_checkpoint"
        ) as persist:
            persist.side_effect = lambda **kwargs: calls.append(kwargs["status"]) or state.PersistStatus(
                True, True, "saved"
            )
            state.install_runtime_persistence_wrapper(orchestrator)
            with self.assertRaises(RuntimeError) as caught:
                orchestrator.produce()

        self.assertIs(caught.exception, root)
        self.assertEqual(calls, ["in_progress"])

    def test_runtime_wrapper_requires_complete_marker_after_success(self) -> None:
        orchestrator = SimpleNamespace(produce=lambda: "ok")
        calls: list[str] = []
        with patch.object(state, "canonical_runtime_enabled", return_value=True), patch.object(
            state, "persist_runtime_checkpoint"
        ) as persist:
            persist.side_effect = lambda **kwargs: calls.append(kwargs["status"]) or state.PersistStatus(
                True, True, "saved"
            )
            state.install_runtime_persistence_wrapper(orchestrator)
            self.assertEqual(orchestrator.produce(), "ok")
        self.assertEqual(calls, ["complete"])

    def test_runtime_secret_materialization_is_owner_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "secret"
            state._write_secret(path, "value")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()

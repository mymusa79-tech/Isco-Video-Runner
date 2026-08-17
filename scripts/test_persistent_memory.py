from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import persistent_memory as pm


class PersistentMemoryTests(unittest.TestCase):
    def test_legacy_pexels_ids_are_preserved_and_projected_to_visual_assets(self):
        data = {"videos": [{"topic": "x", "pexels_ids": [12, "13", None, "bad"]}]}
        got = pm.normalize_history(data)["videos"][0]
        self.assertEqual(got["pexels_ids"], [12, 13])
        self.assertEqual(got["visual_assets"], [{"provider": "pexels", "id": 12}, {"provider": "pexels", "id": 13}])

    def test_new_visual_assets_are_read_into_legacy_projection(self):
        data = {
            "videos": [{
                "visual_assets": [
                    {"provider": "pexels", "id": 41},
                    {"provider": "pexels", "asset_id": "42"},
                    {"provider": "pixabay", "id": 99},
                ]
            }]
        }
        got = pm.normalize_history(data)["videos"][0]
        self.assertEqual(got["pexels_ids"], [41, 42])
        self.assertEqual(got["visual_assets"][2], {"provider": "pixabay", "id": 99})

    def test_mixed_formats_deduplicate_without_losing_non_pexels_assets(self):
        data = {
            "videos": [{
                "pexels_ids": [7, 8],
                "visual_assets": [
                    {"provider": "pexels", "id": 8},
                    {"provider": "pixabay", "id": 1234},
                ],
            }]
        }
        got = pm.normalize_history(data)["videos"][0]
        self.assertEqual(got["pexels_ids"], [7, 8])
        self.assertIn({"provider": "pexels", "id": 7}, got["visual_assets"])
        self.assertIn({"provider": "pixabay", "id": 1234}, got["visual_assets"])

    def test_invalid_history_root_is_rejected(self):
        with self.assertRaises(ValueError):
            pm.normalize_history([])
        with self.assertRaises(ValueError):
            pm.normalize_history({"videos": {}})

    def test_encrypt_decrypt_round_trip_uses_openssl_salted_payload(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            plain = root / "history.json"
            enc = root / "history.json.enc"
            restored = root / "restored.json"
            plain.write_text(json.dumps({"videos": [{"pexels_ids": [5]}]}), encoding="utf-8")
            pm.encrypt_history(plain, enc, "secret-key")
            self.assertTrue(enc.read_bytes().startswith(b"Salted__"))
            status = pm.decrypt_history(enc, restored, "secret-key")
            self.assertTrue(status.save_allowed)
            got = json.loads(restored.read_text(encoding="utf-8"))
            self.assertEqual(got["videos"][0]["pexels_ids"], [5])
            self.assertEqual(got["videos"][0]["visual_assets"], [{"provider": "pexels", "id": 5}])

    def test_bad_decryption_key_locks_save_and_never_keeps_partial_plaintext(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            plain = root / "history.json"
            enc = root / "history.json.enc"
            restored = root / "restored.json"
            plain.write_text(json.dumps({"videos": [{"topic": "valid"}]}), encoding="utf-8")
            pm.encrypt_history(plain, enc, "correct")
            status = pm.decrypt_history(enc, restored, "wrong")
            self.assertFalse(status.save_allowed)
            self.assertEqual(json.loads(restored.read_text(encoding="utf-8")), {"videos": []})
            self.assertFalse((root / "restored.json.decrypting").exists())

    def test_malformed_decrypted_json_locks_save(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bad = root / "bad.json"
            enc = root / "bad.enc"
            out = root / "out.json"
            bad.write_text("not-json", encoding="utf-8")
            env = dict(os.environ)
            env["STATE_ENCRYPTION_KEY"] = "key"
            subprocess.run([
                "openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-salt",
                "-in", str(bad), "-out", str(enc), "-pass", "env:STATE_ENCRYPTION_KEY",
            ], env=env, check=True)
            status = pm.decrypt_history(enc, out, "key")
            self.assertFalse(status.save_allowed)
            self.assertIn("invalid decrypted history", status.reason)
            self.assertEqual(json.loads(out.read_text(encoding="utf-8")), {"videos": []})

    def test_restore_missing_agent_state_uses_legacy_main_state(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
            remote = root / "remote.git"
            subprocess.run(["git", "init", "--bare", str(remote)], check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True)
            legacy_plain = root / "legacy.json"
            legacy_plain.write_text(json.dumps({"videos": [{"pexels_ids": [77]}]}), encoding="utf-8")
            legacy = repo / "state" / "history.json.enc"
            legacy.parent.mkdir()
            pm.encrypt_history(legacy_plain, legacy, "key")
            out = root / "runtime.json"
            status = pm.restore_from_git(repo, out, "key")
            self.assertTrue(status.save_allowed)
            self.assertEqual(status.source, "legacy-main")
            self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["videos"][0]["pexels_ids"], [77])

    def test_restore_agent_state_decrypt_failure_sets_explicit_save_lock(self):
        responses = iter([
            subprocess.CompletedProcess([], 0, b"ref", b""),
            subprocess.CompletedProcess([], 0, b"", b""),
            subprocess.CompletedProcess([], 0, b"not-an-openssl-payload", b""),
            subprocess.CompletedProcess([], 1, b"", b"bad decrypt"),
        ])

        def fake_run(*args, **kwargs):
            return next(responses)

        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "runtime.json"
            status = pm.restore_from_git(Path(td), out, "key", run_cmd=fake_run)
            self.assertFalse(status.save_allowed)
            self.assertEqual(status.source, "agent-state")
            self.assertEqual(json.loads(out.read_text(encoding="utf-8")), {"videos": []})

    def test_restore_transport_failure_locks_save_instead_of_treating_remote_as_empty(self):
        def fake_run(*args, **kwargs):
            return subprocess.CompletedProcess([], 128, b"", b"network down")

        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "runtime.json"
            status = pm.restore_from_git(Path(td), out, "key", run_cmd=fake_run)
            self.assertFalse(status.save_allowed)
            self.assertIn("probe failed", status.reason)

    def test_persist_policy_requires_success_approval_and_restore_lock_clear(self):
        self.assertTrue(pm.should_persist(technical_success=True, approval_decision="approved", restore_save_allowed=True))
        self.assertFalse(pm.should_persist(technical_success=True, approval_decision="hold", restore_save_allowed=True))
        self.assertFalse(pm.should_persist(technical_success=True, approval_decision="approved", restore_save_allowed=False))
        self.assertFalse(pm.should_persist(technical_success=False, approval_decision="approved", restore_save_allowed=True))

    def test_push_rejection_warns_without_retry_or_force(self):
        calls = []
        responses = iter([
            subprocess.CompletedProcess([], 0, b"ref", b""),
            subprocess.CompletedProcess([], 0, b"", b""),
            subprocess.CompletedProcess([], 0, b"", b""),
            subprocess.CompletedProcess([], 0, b"", b""),
            subprocess.CompletedProcess([], 0, b"", b""),
            subprocess.CompletedProcess([], 0, b"", b""),
            subprocess.CompletedProcess([], 1, b"", b""),
            subprocess.CompletedProcess([], 0, b"", b""),
            subprocess.CompletedProcess([], 1, b"", b"rejected"),
        ])

        def fake_run(args, **kwargs):
            calls.append(list(args))
            return next(responses)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            encrypted = root / "history.json.enc"
            encrypted.write_bytes(b"Salted__" + b"x" * 32)
            status = pm.persist_encrypted_state(root, encrypted, run_number="29", run_cmd=fake_run)
            self.assertFalse(status.pushed)
            self.assertTrue(status.changed)
            pushes = [cmd for cmd in calls if len(cmd) > 1 and cmd[1] == "push"]
            self.assertEqual(len(pushes), 1)
            command = pushes[0]
            self.assertNotIn("--force", command)
            self.assertNotIn("--force-with-lease", command)
            self.assertFalse(any(arg.startswith("+") for arg in command))

    def test_first_persist_creates_orphan_branch_not_main_history_commit(self):
        calls = []
        responses = iter([
            subprocess.CompletedProcess([], 2, b"", b""),
            subprocess.CompletedProcess([], 0, b"", b""),
            subprocess.CompletedProcess([], 0, b"", b""),
            subprocess.CompletedProcess([], 0, b"", b""),
            subprocess.CompletedProcess([], 0, b"", b""),
            subprocess.CompletedProcess([], 0, b"", b""),
            subprocess.CompletedProcess([], 1, b"", b""),
            subprocess.CompletedProcess([], 0, b"", b""),
            subprocess.CompletedProcess([], 0, b"", b""),
        ])

        def fake_run(args, **kwargs):
            calls.append(list(args))
            return next(responses)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            encrypted = root / "payload.enc"
            encrypted.write_bytes(b"Salted__" + b"y" * 32)
            status = pm.persist_encrypted_state(root, encrypted, run_cmd=fake_run)
            self.assertTrue(status.pushed)
            self.assertIn(["git", "checkout", "--orphan", "agent-state"], calls)
            self.assertIn(["git", "push", "origin", "HEAD:refs/heads/agent-state"], calls)


if __name__ == "__main__":
    unittest.main()

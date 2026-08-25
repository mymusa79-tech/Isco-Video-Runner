from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import persistent_memory as pm
from scripts import persistent_memory_crypto as crypto


class PersistentMemoryTests(unittest.TestCase):
    def test_legacy_pexels_ids_are_preserved_and_projected_to_visual_assets(self):
        data = {"videos": [{"topic": "x", "pexels_ids": [12, "13", None, "bad"]}]}
        got = pm.normalize_history(data)["videos"][0]
        self.assertEqual(got["pexels_ids"], [12, 13])
        self.assertEqual(got["visual_assets"], [{"provider": "pexels", "id": 12}, {"provider": "pexels", "id": 13}])

    def test_new_visual_assets_are_read_into_legacy_projection(self):
        data = {"videos": [{"visual_assets": [
            {"provider": "pexels", "id": 41},
            {"provider": "pexels", "asset_id": "42"},
            {"provider": "pixabay", "id": 99},
        ]}]}
        got = pm.normalize_history(data)["videos"][0]
        self.assertEqual(got["pexels_ids"], [41, 42])
        self.assertEqual(got["visual_assets"][2], {"provider": "pixabay", "id": 99})

    def test_invalid_history_root_is_rejected(self):
        with self.assertRaises(ValueError):
            pm.normalize_history([])
        with self.assertRaises(ValueError):
            pm.normalize_history({"videos": {}})

    def _make_v2(self, root: Path, *, key: str = "secret-key", run: str = "17", previous: str = "none") -> tuple[Path, Path]:
        plain = root / "history.json"
        enc = root / "history.json.enc"
        plain.write_text(json.dumps({"videos": [{"pexels_ids": [5]}]}), encoding="utf-8")
        pm.encrypt_history(plain, enc, key, run_number=run, previous_state_commit=previous)
        return plain, enc

    def test_encrypt_decrypt_round_trip_uses_authenticated_aes_gcm_envelope(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _, enc = self._make_v2(root)
            payload = json.loads(enc.read_text(encoding="utf-8"))
            self.assertEqual(payload["format"], crypto.FORMAT)
            self.assertEqual(payload["version"], 2)
            self.assertEqual(payload["cipher"]["name"], "aes-256-gcm")
            self.assertEqual(payload["kdf"]["name"], "pbkdf2-hmac-sha256")
            self.assertEqual(payload["metadata"]["sequence"], 17)
            restored = root / "restored.json"
            status = pm.decrypt_history(enc, restored, "secret-key")
            self.assertTrue(status.save_allowed)
            self.assertEqual(status.state_sequence, 17)
            got = json.loads(restored.read_text(encoding="utf-8"))
            self.assertEqual(got["videos"][0]["pexels_ids"], [5])

    def test_wrong_key_locks_save_and_never_keeps_partial_plaintext(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _, enc = self._make_v2(root, key="correct")
            out = root / "restored.json"
            status = pm.decrypt_history(enc, out, "wrong")
            self.assertFalse(status.save_allowed)
            self.assertIn("authentication tag", status.reason)
            self.assertEqual(json.loads(out.read_text(encoding="utf-8")), {"videos": []})

    def test_ciphertext_tamper_fails_before_plaintext_is_trusted(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _, enc = self._make_v2(root)
            envelope = json.loads(enc.read_text(encoding="utf-8"))
            value = envelope["cipher"]["ciphertext_b64"]
            envelope["cipher"]["ciphertext_b64"] = ("A" if value[0] != "A" else "B") + value[1:]
            enc.write_text(json.dumps(envelope), encoding="utf-8")
            out = root / "out.json"
            status = pm.decrypt_history(enc, out, "secret-key")
            self.assertFalse(status.save_allowed)
            self.assertEqual(json.loads(out.read_text(encoding="utf-8")), {"videos": []})

    def test_authenticated_metadata_tamper_fails(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _, enc = self._make_v2(root, run="21")
            envelope = json.loads(enc.read_text(encoding="utf-8"))
            envelope["metadata"]["run_number"] = "22"
            envelope["metadata"]["sequence"] = 22
            enc.write_text(json.dumps(envelope), encoding="utf-8")
            out = root / "out.json"
            status = pm.decrypt_history(enc, out, "secret-key")
            self.assertFalse(status.save_allowed)
            self.assertIn("authentication tag", status.reason)

    def test_truncated_or_non_envelope_payload_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            enc = root / "history.enc"
            out = root / "out.json"
            enc.write_bytes(b'{"format":"isco-agent-state","version":2}')
            status = pm.decrypt_history(enc, out, "key")
            self.assertFalse(status.save_allowed)
            self.assertEqual(json.loads(out.read_text(encoding="utf-8")), {"videos": []})

    def test_arbitrary_legacy_cbc_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            plain = root / "legacy.json"
            enc = root / "legacy.enc"
            out = root / "out.json"
            plain.write_text(json.dumps({"videos": [{"topic": "legacy"}]}), encoding="utf-8")
            env = dict(os.environ)
            env["STATE_ENCRYPTION_KEY"] = "key"
            subprocess.run([
                "openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-salt",
                "-in", str(plain), "-out", str(enc), "-pass", "env:STATE_ENCRYPTION_KEY",
            ], env=env, check=True)
            status = pm.decrypt_history(enc, out, "key", legacy_blob_sha="0" * 40)
            self.assertFalse(status.save_allowed)
            self.assertIn("not an approved one-time migration blob", status.reason)

    def test_pinned_legacy_cbc_can_migrate_once(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            plain = root / "legacy.json"
            enc = root / "legacy.enc"
            out = root / "out.json"
            plain.write_text(json.dumps({"videos": [{"pexels_ids": [77]}]}), encoding="utf-8")
            env = dict(os.environ)
            env["STATE_ENCRYPTION_KEY"] = "key"
            subprocess.run([
                "openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-salt",
                "-in", str(plain), "-out", str(enc), "-pass", "env:STATE_ENCRYPTION_KEY",
            ], env=env, check=True)
            synthetic_pin = "a" * 40
            with patch.object(pm, "APPROVED_LEGACY_BLOB_SHAS", frozenset({synthetic_pin})):
                status = pm.decrypt_history(enc, out, "key", legacy_blob_sha=synthetic_pin)
            self.assertTrue(status.save_allowed)
            self.assertEqual(status.source, "encrypted-legacy-migration")
            self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["videos"][0]["pexels_ids"], [77])

    def test_restore_identity_sidecar_binds_later_encrypt_without_workflow_rewire(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            plain = root / "history.json"
            plain.write_text(json.dumps({"videos": []}), encoding="utf-8")
            status = pm.RestoreStatus(True, "agent-state", state_commit="1" * 40, state_sequence=9)
            pm.write_restore_identity(plain, status)
            with patch.dict(os.environ, {"GITHUB_RUN_NUMBER": "10"}, clear=False):
                enc = root / "history.enc"
                pm.encrypt_history(plain, enc, "key")
            _, metadata = crypto.open_envelope(enc.read_bytes(), "key")
            self.assertEqual(metadata.sequence, 10)
            self.assertEqual(metadata.previous_state_commit, "1" * 40)

    def test_missing_restore_identity_fails_closed_for_implicit_production_encrypt(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            plain = root / "history.json"
            plain.write_text(json.dumps({"videos": []}), encoding="utf-8")
            with patch.dict(os.environ, {"GITHUB_RUN_NUMBER": "10"}, clear=False):
                with self.assertRaises((OSError, ValueError)):
                    pm.encrypt_history(plain, root / "x.enc", "key")

    def test_authenticated_state_restore_verifies_commit_subject_parent_and_age(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            remote = root / "remote.git"
            repo = root / "repo"
            subprocess.run(["git", "init", "--bare", str(remote)], check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "init", str(repo)], check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True)
            plain = root / "plain.json"
            enc = root / "state.enc"
            plain.write_text(json.dumps({"videos": [{"pexels_ids": [8]}]}), encoding="utf-8")
            pm.encrypt_history(plain, enc, "key", run_number="5", previous_state_commit="none")
            persisted = pm.persist_encrypted_state(repo, enc, run_number="5", key="key")
            self.assertTrue(persisted.pushed)
            out = root / "restored.json"
            status = pm.restore_from_git(repo, out, "key", current_run_number="6")
            self.assertTrue(status.save_allowed)
            self.assertEqual(status.state_sequence, 5)
            self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["videos"][0]["pexels_ids"], [8])
            blocked = pm.restore_from_git(repo, root / "blocked.json", "key", current_run_number="5")
            self.assertFalse(blocked.save_allowed)
            self.assertIn("not older", blocked.reason)

    def test_persist_rejects_stale_authenticated_ancestry(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _, enc = self._make_v2(root, run="29", previous="a" * 40)
            responses = iter([
                subprocess.CompletedProcess([], 0, b"ref", b""),
                subprocess.CompletedProcess([], 0, b"", b""),
                subprocess.CompletedProcess([], 0, ("b" * 40 + "\n").encode(), b""),
            ])
            def fake_run(args, **kwargs):
                return next(responses)
            status = pm.persist_encrypted_state(root, enc, run_number="29", key="secret-key", run_cmd=fake_run)
            self.assertFalse(status.pushed)
            self.assertIn("ancestry is stale", status.reason)

    def test_push_rejection_warns_without_retry_or_force(self):
        calls = []
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            current = "c" * 40
            _, enc = self._make_v2(root, run="29", previous=current)
            responses = iter([
                subprocess.CompletedProcess([], 0, b"ref", b""),
                subprocess.CompletedProcess([], 0, b"", b""),
                subprocess.CompletedProcess([], 0, (current + "\n").encode(), b""),
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
            status = pm.persist_encrypted_state(root, enc, run_number="29", key="secret-key", run_cmd=fake_run)
            self.assertFalse(status.pushed)
            self.assertTrue(status.changed)
            pushes = [cmd for cmd in calls if len(cmd) > 1 and cmd[1] == "push"]
            self.assertEqual(len(pushes), 1)
            self.assertNotIn("--force", pushes[0])
            self.assertNotIn("--force-with-lease", pushes[0])
            self.assertFalse(any(arg.startswith("+") for arg in pushes[0]))

    def test_first_persist_creates_orphan_branch_and_authenticated_commit(self):
        calls = []
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _, enc = self._make_v2(root, run="31", previous="none")
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
            status = pm.persist_encrypted_state(root, enc, run_number="31", key="secret-key", run_cmd=fake_run)
            self.assertTrue(status.pushed)
            self.assertIn(["git", "checkout", "--orphan", "agent-state"], calls)
            self.assertIn(["git", "commit", "-m", "Update authenticated agent state (run 31)"], calls)
            self.assertIn(["git", "push", "origin", "HEAD:refs/heads/agent-state"], calls)

    def test_persist_policy_requires_success_approval_and_restore_lock_clear(self):
        self.assertTrue(pm.should_persist(technical_success=True, approval_decision="approved", restore_save_allowed=True))
        self.assertFalse(pm.should_persist(technical_success=True, approval_decision="hold", restore_save_allowed=True))
        self.assertFalse(pm.should_persist(technical_success=True, approval_decision="approved", restore_save_allowed=False))
        self.assertFalse(pm.should_persist(technical_success=False, approval_decision="approved", restore_save_allowed=True))


if __name__ == "__main__":
    unittest.main()

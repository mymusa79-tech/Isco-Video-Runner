from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts import persistent_memory as pm


class PersistentMemoryRegressionPreservationTests(unittest.TestCase):
    def test_mixed_formats_deduplicate_without_losing_non_pexels_assets(self) -> None:
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

    def test_restore_transport_failure_still_locks_save(self) -> None:
        def fake_run(*args, **kwargs):
            return subprocess.CompletedProcess([], 128, b"", b"network down")

        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "runtime.json"
            status = pm.restore_from_git(Path(td), out, "key", run_cmd=fake_run)
            self.assertFalse(status.save_allowed)
            self.assertIn("probe failed", status.reason)
            self.assertEqual(json.loads(out.read_text(encoding="utf-8")), {"videos": []})

    def test_unapproved_remote_legacy_blob_locks_save_before_decrypt(self) -> None:
        commit = "1" * 40
        wrong_blob = "2" * 40
        responses = iter([
            subprocess.CompletedProcess([], 0, b"ref", b""),
            subprocess.CompletedProcess([], 0, b"", b""),
            subprocess.CompletedProcess([], 0, b"Salted__" + b"x" * 64, b""),
            subprocess.CompletedProcess([], 0, (commit + "\n").encode(), b""),
            subprocess.CompletedProcess([], 0, (wrong_blob + "\n").encode(), b""),
            subprocess.CompletedProcess([], 0, b"tree deadbeef\n", b""),
            subprocess.CompletedProcess([], 0, b"legacy subject\n", b""),
        ])
        calls: list[list[str]] = []

        def fake_run(args, **kwargs):
            calls.append(list(args))
            return next(responses)

        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "runtime.json"
            status = pm.restore_from_git(Path(td), out, "key", run_cmd=fake_run)
            self.assertFalse(status.save_allowed)
            self.assertEqual(status.source, "agent-state")
            self.assertIn("not an approved one-time migration blob", status.reason)
            self.assertEqual(json.loads(out.read_text(encoding="utf-8")), {"videos": []})
            self.assertFalse(any(cmd[:3] == ["openssl", "enc", "-d"] for cmd in calls))

    def test_two_authenticated_state_generations_restore_through_depth_one_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            remote = root / "remote.git"
            repo = root / "repo"
            subprocess.run(["git", "init", "--bare", str(remote)], check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "init", str(repo)], check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True)

            plain = root / "history.json"
            first = root / "state-5.enc"
            plain.write_text(json.dumps({"videos": [{"pexels_ids": [5]}]}), encoding="utf-8")
            pm.encrypt_history(plain, first, "key", run_number="5", previous_state_commit="none")
            self.assertTrue(pm.persist_encrypted_state(repo, first, run_number="5", key="key").pushed)

            restored5 = root / "restored-5.json"
            status5 = pm.restore_from_git(repo, restored5, "key", current_run_number="6")
            self.assertTrue(status5.save_allowed)
            self.assertEqual(status5.state_sequence, 5)
            self.assertEqual(len(status5.state_commit), 40)

            second = root / "state-6.enc"
            restored5.write_text(json.dumps({"videos": [{"pexels_ids": [5, 6]}]}), encoding="utf-8")
            pm.encrypt_history(
                restored5,
                second,
                "key",
                run_number="6",
                previous_state_commit=status5.state_commit,
            )
            self.assertTrue(pm.persist_encrypted_state(repo, second, run_number="6", key="key").pushed)

            restored6 = root / "restored-6.json"
            status6 = pm.restore_from_git(repo, restored6, "key", current_run_number="7")
            self.assertTrue(status6.save_allowed)
            self.assertEqual(status6.state_sequence, 6)
            self.assertEqual(status6.previous_state_commit, status5.state_commit)
            self.assertEqual(json.loads(restored6.read_text(encoding="utf-8"))["videos"][0]["pexels_ids"], [5, 6])


if __name__ == "__main__":
    unittest.main()

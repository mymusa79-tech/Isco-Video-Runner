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
            subprocess.CompletedProcess([], 0, b"", b""),
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


if __name__ == "__main__":
    unittest.main()

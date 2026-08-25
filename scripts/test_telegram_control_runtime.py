from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import telegram_topic_memory_ui as topic_ui


ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "scripts" / "telegram_topic_memory_ui.py"


class TelegramControlRuntimeTests(unittest.TestCase):
    def test_exact_direct_entrypoint_imports_from_repository_root(self) -> None:
        result = subprocess.run(
            [sys.executable, str(ENTRYPOINT), "--help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("ModuleNotFoundError", result.stderr)
        self.assertIn("Telegram editorial control plane", result.stdout)

    def test_poll_fails_closed_when_authorization_boundary_is_incomplete(self) -> None:
        env = os.environ.copy()
        for name in topic_ui._REQUIRED_POLL_SECRET_FILES:
            env.pop(name, None)
        result = subprocess.run(
            [sys.executable, str(ENTRYPOINT), "poll", "--state", str(ROOT / "state" / "unused-test-control.json")],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("authorization boundary is incomplete", result.stderr)
        for name in topic_ui._REQUIRED_POLL_SECRET_FILES:
            self.assertIn(name, result.stderr)

    def test_poll_identity_preflight_accepts_complete_secret_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            mapping: dict[str, str] = {}
            for index, name in enumerate(topic_ui._REQUIRED_POLL_SECRET_FILES, start=1):
                path = root / f"secret-{index}"
                path.write_text(str(index), encoding="utf-8")
                mapping[name] = str(path)
            with patch.dict(os.environ, mapping, clear=False):
                topic_ui._require_poll_identity("poll")


if __name__ == "__main__":
    unittest.main()

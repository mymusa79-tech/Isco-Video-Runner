from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 40


class CertificationResumeEntrypointTests(unittest.TestCase):
    def test_direct_workflow_cli_can_import_scripts_package(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            state = tmp / "control-panel.json"
            github_output = tmp / "github-output.txt"
            state.write_text(json.dumps({"production_queue": []}), encoding="utf-8")

            env = dict(os.environ)
            env.pop("PYTHONPATH", None)
            completed = subprocess.run(
                [
                    sys.executable,
                    "scripts/telegram_certification_resume.py",
                    "--state",
                    str(state),
                    "--runner-sha",
                    SHA,
                    "--github-output",
                    str(github_output),
                ],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(completed.stdout), {"found": False})
            self.assertEqual(github_output.read_text(encoding="utf-8"), "found=false\n")


if __name__ == "__main__":
    unittest.main()

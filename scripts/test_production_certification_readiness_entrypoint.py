from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
HELP_MARKER = "Bounded exact-SHA production certification readiness wait"


class ProductionCertificationReadinessEntrypointTests(unittest.TestCase):
    def _run_without_pythonpath(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        return subprocess.run(
            [sys.executable, *args, "--help"],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_direct_workflow_cli_can_import_scripts_package(self) -> None:
        completed = self._run_without_pythonpath(
            "scripts/production_certification_readiness.py"
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn(HELP_MARKER, completed.stdout)

    def test_module_entrypoint_remains_valid_without_pythonpath(self) -> None:
        completed = self._run_without_pythonpath(
            "-m", "scripts.production_certification_readiness"
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn(HELP_MARKER, completed.stdout)

    def test_telegram_gateway_still_exercises_the_direct_entrypoint(self) -> None:
        workflow = (
            REPO_ROOT / ".github/workflows/telegram-production-request.yml"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "python scripts/production_certification_readiness.py \\",
            workflow,
        )


if __name__ == "__main__":
    unittest.main()

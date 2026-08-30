from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/telegram-editorial-control.yml"


class TelegramDirectEntrypointTests(unittest.TestCase):
    def test_workflow_direct_scripts_restore_repo_root_before_package_imports(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        names = sorted(set(re.findall(r"python scripts/([A-Za-z0-9_]+\.py)", workflow)))
        offenders: list[str] = []
        for name in names:
            path = ROOT / "scripts" / name
            text = path.read_text(encoding="utf-8")
            if "from scripts" not in text and "import scripts" not in text:
                continue
            if 'if __package__ in {None, ""}:' not in text or "sys.path.insert" not in text:
                offenders.append(name)
        self.assertEqual(offenders, [], f"workflow direct-entrypoint import drift: {offenders}")

    def test_runtime_failed_entrypoints_boot_exactly_as_workflow_invokes_them(self):
        for relative in (
            "scripts/telegram_webhook_replay.py",
            "scripts/telegram_status_projection.py",
        ):
            with self.subTest(relative=relative):
                result = subprocess.run(
                    [sys.executable, relative, "--help"],
                    cwd=ROOT,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stdout)


if __name__ == "__main__":
    unittest.main()

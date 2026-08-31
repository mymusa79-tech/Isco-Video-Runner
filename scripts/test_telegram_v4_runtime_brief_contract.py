from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class TelegramV4RuntimeBriefContractTests(unittest.TestCase):
    def test_direct_reconciliation_bootstrap_precedes_scripts_import(self) -> None:
        ingress = ROOT / "scripts" / "telegram_v4_ingress.py"
        ingress_text = ingress.read_text(encoding="utf-8")
        ast.parse(ingress_text, filename=str(ingress))
        bootstrap = ingress_text.index("sys.path.insert")
        queue_import = ingress_text.index("from scripts.telegram_production_queue import")
        self.assertLess(
            bootstrap,
            queue_import,
            "Telegram V4 direct-execution import bootstrap occurs too late",
        )

    def test_runtime_brief_closure_preserves_strict_hermeticity(self) -> None:
        snapshot = ROOT / "scripts" / "immutable_planning_snapshot.py"
        snapshot_text = snapshot.read_text(encoding="utf-8")
        ast.parse(snapshot_text, filename=str(snapshot))
        required = (
            "_telegram_runtime_approved_brief_bytes",
            "verify_brief_approval",
            'git", "checkout", "--", _COMMITTED_BRIEF_PATH',
            "engine_worktree_restored=true",
            "production_source=runtime_snapshot",
        )
        for item in required:
            self.assertIn(item, snapshot_text, f"Telegram runtime brief closure missing: {item}")

        hermeticity = (ROOT / "scripts" / "engine_source_hermeticity.py").read_text(encoding="utf-8")
        self.assertNotIn(
            "approved_brief.json",
            hermeticity,
            "Engine hermeticity was weakened with an approved-brief exception",
        )


if __name__ == "__main__":
    unittest.main()

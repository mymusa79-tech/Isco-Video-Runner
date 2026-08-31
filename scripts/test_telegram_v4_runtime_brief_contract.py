from __future__ import annotations

import ast
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent


class TelegramV4RuntimeBriefContractTests(unittest.TestCase):
    @staticmethod
    def _git(cwd: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return result.stdout.strip()

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
        snapshot_path = ROOT / "scripts" / "immutable_planning_snapshot.py"
        snapshot_text = snapshot_path.read_text(encoding="utf-8")
        ast.parse(snapshot_text, filename=str(snapshot_path))
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

    def test_telegram_dynamic_brief_moves_to_runtime_snapshot_and_engine_returns_clean(self) -> None:
        from isco_video_agent.brief_approval_binding import attach_approval_binding
        from scripts import immutable_planning_snapshot as snapshot

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            engine = root / "engine"
            engine.mkdir()
            self._git(engine, "init")
            self._git(engine, "config", "user.name", "test")
            self._git(engine, "config", "user.email", "test@example.com")

            committed_path = engine / "production" / "approved_brief.json"
            committed_path.parent.mkdir(parents=True)
            committed_bytes = b'{"approved_by_user":true,"approved_topic":"manual","format":"film"}\n'
            committed_path.write_bytes(committed_bytes)
            self._git(engine, "add", ".")
            self._git(engine, "commit", "-m", "engine")

            request_id = "req-telegram-runtime-test"
            request_sha = "a" * 64
            request_path = root / "approved-request.json"
            request_path.write_text(
                json.dumps(
                    {
                        "request_id": request_id,
                        "request_sha256": request_sha,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            dynamic = attach_approval_binding(
                {
                    "approved_by_user": True,
                    "approved_topic": "telegram dynamic",
                    "format": "film",
                    "research_pack": [],
                    "content_boundaries": [],
                    "control_request_id": request_id,
                    "control_request_sha256": request_sha,
                }
            )
            dynamic_bytes = json.dumps(dynamic, ensure_ascii=False, indent=2).encode("utf-8")
            committed_path.write_bytes(dynamic_bytes)
            self.assertNotEqual(committed_path.read_bytes(), committed_bytes)

            runner_temp = root / "runner-temp"
            runner_temp.mkdir()
            env = {
                "RUNNER_TEMP": str(runner_temp),
                "ISCO_CONTROL_REQUEST_PATH": str(request_path),
                "ISCO_CONTROL_REQUEST_SHA256": request_sha,
            }
            with patch.dict(os.environ, env, clear=False), patch.object(
                snapshot, "canonical_runtime_enabled", return_value=True
            ):
                runtime_snapshot = snapshot.materialize_runtime_snapshot(
                    root,
                    engine,
                    persist_workflow_env=False,
                )

                self.assertEqual(runtime_snapshot.read_bytes(), dynamic_bytes)
                self.assertEqual(runtime_snapshot.stat().st_mode & 0o222, 0)
                self.assertEqual(committed_path.read_bytes(), committed_bytes)
                self.assertEqual(
                    self._git(engine, "status", "--porcelain=v1", "--untracked-files=no"),
                    "",
                )


if __name__ == "__main__":
    unittest.main()

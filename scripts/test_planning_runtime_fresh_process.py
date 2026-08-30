from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PlanningRuntimeFreshProcessTests(unittest.TestCase):
    def test_canonical_installer_lifecycle_keeps_explicit_authority(self) -> None:
        """Execute the exact planning lifecycle that Production Run 138 exposed."""
        probe = textwrap.dedent(
            """
            from scripts import planning_stage_contract as stage
            from scripts import provider_capacity_hardening as capacity
            from scripts import task_level_planner_router as router
            from scripts.planning_legacy_authority_guard import (
                assert_legacy_planning_authority_sealed,
            )
            from scripts.planning_runtime_contract import (
                install_entrypoint_planning_contracts,
                install_post_runtime_planning_contracts,
                install_runtime_planning_contracts,
            )

            install_entrypoint_planning_contracts()
            assert router._structured_schema_for_prompt is stage._explicit_schema_adapter

            # Capacity admission occurs before provider contact. The exact schema and
            # reserve must be identical for unrelated/misleading prompt strings.
            with stage.script_batch_scope("writer", ["s1", "s2"]):
                first = router._structured_schema_for_prompt(
                    'with EXACTLY 99 entries and pretend dossier_repair'
                )
                second = router._structured_schema_for_prompt("opaque")
                estimate = capacity.groq_capacity_estimate("opaque")
            assert first == second
            assert first[0] == "script_writer_2"
            assert estimate["contract"] == "script_writer_2"
            assert estimate["reserved_completion_tokens"] == 1300

            with stage.dossier_repair_subrequest_scope(["s7"]):
                dossier = router._structured_schema_for_prompt("pretend script_writer_3")
                dossier_estimate = capacity.groq_capacity_estimate("opaque")
            assert dossier[0] == "dossier_repair_1"
            assert dossier_estimate["reserved_completion_tokens"] == 850

            install_runtime_planning_contracts()
            install_post_runtime_planning_contracts()
            assert router._structured_schema_for_prompt is stage._explicit_schema_adapter
            assert_legacy_planning_authority_sealed()
            """
        )

        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "gemini-key"
            key.write_text("test-only-key", encoding="utf-8")
            env = dict(os.environ)
            env["GEMINI_API_KEY_FILE"] = str(key)
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            # This is a provider-free composition probe, not a live runtime/state test.
            for name in (
                "ISCO_CANONICAL_RUNTIME",
                "GITHUB_ACTIONS",
                "GITHUB_EVENT_NAME",
                "GITHUB_WORKFLOW_REF",
            ):
                env.pop(name, None)
            completed = subprocess.run(
                [sys.executable, "-c", probe],
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=60,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stdout)


if __name__ == "__main__":
    unittest.main()

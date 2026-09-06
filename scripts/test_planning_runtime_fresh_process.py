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
            import os
            from pathlib import Path

            import isco_video_agent.resilient_planner as staged
            from isco_video_agent.config import secret
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

            router.CACHE_PATH = Path(os.environ["ISCO_TEST_TMP"]) / "planning-checkpoint.json"
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

            # Match canonical Production exactly: run_v3_voice consumes the one-time
            # file first, then Engine config.secret() consumes the direct env copy and
            # passes the key in process to resilient_planner.json_text().
            entrypoint_key = secret("GEMINI_API_KEY")
            assert entrypoint_key == "test-only-key"
            assert "GEMINI_API_KEY_FILE" not in os.environ
            assert not Path(os.environ["ISCO_TEST_SECRET_PATH"]).exists()
            os.environ["GEMINI_API_KEY"] = entrypoint_key
            request_key = secret("GEMINI_API_KEY")
            assert request_key == "test-only-key"
            assert "GEMINI_API_KEY" not in os.environ

            seen = {}
            def fake_gemini(api_key, prompt, model="gemini-2.5-flash", **kwargs):
                seen["api_key"] = api_key
                return {
                    "sections": [
                        {"id": "s1", "narration": "نص صالح", "key_point": "فكرة"}
                    ]
                }

            router.gemini_json_text = fake_gemini
            with stage.request_stage_scope(stage.script_stage_spec("full_script", ["s1"])):
                payload = staged.json_text(request_key, "opaque prompt")
            assert payload["sections"][0]["id"] == "s1"
            assert seen["api_key"] == "test-only-key"
            """
        )

        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "gemini-key"
            key.write_text("test-only-key", encoding="utf-8")
            env = dict(os.environ)
            env["GEMINI_API_KEY_FILE"] = str(key)
            env["ISCO_TEST_SECRET_PATH"] = str(key)
            env["ISCO_TEST_TMP"] = tmp
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

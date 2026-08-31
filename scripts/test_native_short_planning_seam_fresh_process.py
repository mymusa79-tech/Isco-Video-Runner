from __future__ import annotations

import subprocess
import sys
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class NativeShortPlanningSeamFreshProcessTests(unittest.TestCase):
    """Regression for the real 2026-08-31 Telegram Short production failure.

    Once execute_control_request correctly installed native_short_planner_router (the
    install_router fix in #452) and GEMINI_CONTENT_MODEL matched the canonical model
    (#454), the very next real Telegram Short run failed with:

        RuntimeError: Gemini planning/editorial review failed: {'type': 'PlanningStageError'}

    Root cause: native_short_planner_router.install_native_short_router() binds Engine's
    native Short planner (isco_video_agent.planner.build_plan) to task_router - the
    legacy/compatibility provider mesh in task_level_planner_router.py - because the
    native Short planner predates, and was never updated for, the Explicit Planning
    Stage Contract system built for the long-form path. task_router (and several
    capacity/telemetry helpers it and its installed replacements call:
    provider_capacity_hardening.py, dynamic_planning_capacity.py,
    gemini_planning_output_guard.py, run125_capacity_routing_closure.py) ask a shared,
    module-level _structured_schema_for_prompt()/router._structured_schema_for_prompt()
    for a best-effort schema hint. install_planning_contract_router() replaces that
    resolver process-wide with one that hard-fails outside an active explicit request
    contract - correct for the contract-bound long-form path, but native_short never
    binds one, so every one of those best-effort lookups blew up instead of degrading to
    "no hint" like the original resolver did. install_legacy_planning_authority_guard()
    separately seals task_router's own checkpoint persistence, which used to silently
    discard an already-successful provider result as if the provider itself had failed.

    This is a real, non-mocked-away, fresh-process reproduction (the install functions
    perform real global monkeypatching that must not leak into the rest of the test
    suite) proving the fixed call chain actually completes, with the real (not
    swallowed) provider mesh in play and only the network call mocked.
    """

    def test_native_short_json_text_completes_through_the_real_install_chain(self) -> None:
        probe = textwrap.dedent(
            """
            import tempfile
            from pathlib import Path
            from unittest.mock import patch

            import isco_video_agent.planner as native_short
            from scripts import task_level_planner_router as router
            from scripts.native_short_planner_router import install_native_short_router
            from scripts.planning_runtime_contract import (
                install_entrypoint_planning_contracts,
                install_runtime_planning_contracts,
                install_post_runtime_planning_contracts,
            )
            import scripts.planning_runtime_contract as prc

            with tempfile.TemporaryDirectory() as tmp:
                router.CACHE_PATH = Path(tmp) / "planning-checkpoint.json"
                prc.install_router = install_native_short_router

                install_entrypoint_planning_contracts()
                install_runtime_planning_contracts()
                install_post_runtime_planning_contracts()

                with patch.object(router, "gemini_json_text", return_value='{"sections": []}') as gemini_mock:
                    result = native_short.json_text("fake-key", "test prompt", model="gemini-3.7-flash")

                assert result == {"sections": []}, result
                assert gemini_mock.call_count == 1, gemini_mock.call_count
            """
        )
        completed = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=60,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout)


if __name__ == "__main__":
    unittest.main()

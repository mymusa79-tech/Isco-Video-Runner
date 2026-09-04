from __future__ import annotations

import subprocess
import sys
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ShortPlanningPortCompositionTests(unittest.TestCase):
    """Regression for the exact native-Short production planning composition.

    The test intentionally installs the same entrypoint/runtime planning stack that
    production uses and enters through orchestrator.build_plan. This is the seam that
    previously passed isolated Stage/Capacity tests but failed in production because
    Capacity bypassed Engine-owned named Draft/Review operation context.
    """

    def test_canonical_runtime_short_build_reaches_named_engine_operations(self) -> None:
        probe = textwrap.dedent(
            """
            import json
            import tempfile
            from pathlib import Path
            from unittest.mock import patch

            import isco_video_agent.orchestrator as orchestrator
            import scripts.planning_runtime_contract as prc
            import scripts.short_planning_port_adapter as adapter
            from isco_video_agent.short_planning_port import SHORT_PLANNING_PORT_CONTRACT_ID
            from scripts import task_level_planner_router as router
            from scripts.native_short_planner_router import install_native_short_router
            from scripts.planning_runtime_contract import (
                install_entrypoint_planning_contracts,
                install_runtime_planning_contracts,
            )

            topic = "كيف تنهض عندما تفقد الدافع تمامًا؟"
            payload = {
                "topic": topic,
                "pillar": "rise",
                "format": "moment",
                "hook": "حين يختفي الدافع، ماذا يبقى؟",
                "title_options": ["العنوان أ", "العنوان ب", "العنوان ج"],
                "thumbnail_concepts": ["concept a", "concept b", "concept c"],
                "sections": [{
                    "id": "s1",
                    "narration": "",
                    "visual_query": "person sitting quietly near window morning light",
                    "on_screen_text": "لا تنتظر عودة الدافع",
                    "emotion": "reflective",
                    "expected_seconds": 15.0,
                    "key_point": "ابدأ بحركة صغيرة قبل عودة الشعور",
                }],
                "cta": "",
                "closing_payoff": "الحركة الصغيرة قد تسبق الشعور.",
            }

            with tempfile.TemporaryDirectory() as tmp:
                router.CACHE_PATH = Path(tmp) / "planning-checkpoint.json"
                # Canonical standalone-Short production selects the native Short router
                # before entrypoint contracts bind the final orchestrator lifecycle.
                prc.install_router = install_native_short_router

                install_entrypoint_planning_contracts()
                install_runtime_planning_contracts()

                assert adapter._INSTALLED, "Short Planning Port adapter was not installed"
                assert SHORT_PLANNING_PORT_CONTRACT_ID == "engine.short_planning_port.v1"

                responses = [
                    json.dumps(payload, ensure_ascii=False),
                    json.dumps(payload, ensure_ascii=False),
                ]
                with patch.object(router, "gemini_json_text", side_effect=responses) as gemini_mock:
                    plan = orchestrator.build_plan(
                        "fake-key",
                        topic,
                        "moment",
                        "gemini-3.7-flash",
                        research_context={},
                        avoid_context={},
                        revision_note="",
                    )

                assert plan.format == "moment", plan.format
                assert len(plan.sections) == 1, len(plan.sections)
                assert gemini_mock.call_count == 2, gemini_mock.call_count
                assert router.CACHE_PATH.is_file(), "named Short stages did not persist checkpoint"
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

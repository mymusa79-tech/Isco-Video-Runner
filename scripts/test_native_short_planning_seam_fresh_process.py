from __future__ import annotations

import subprocess
import sys
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class NativeShortPlanningSeamFreshProcessTests(unittest.TestCase):
    """Regression for the real Telegram Short planning seam in a fresh process.

    The historical regression proved that the native Short provider mesh could still
    complete after the long-form Explicit Planning Stage Contract was installed. With
    the native Short Stage Contract now authoritative, an unowned direct provider call
    is intentionally no longer a supported compatibility path: it must fail closed.
    The same provider call must complete once an explicit native Short stage is active.

    Keeping this as a subprocess matters because the production installers perform
    real process-global monkeypatching that must not leak into the rest of the suite.
    Only the network boundary is mocked.
    """

    def test_native_short_json_text_requires_and_completes_through_explicit_stage(self) -> None:
        probe = textwrap.dedent(
            """
            import json
            import tempfile
            from pathlib import Path
            from unittest.mock import patch

            import isco_video_agent.planner as native_short
            from scripts import task_level_planner_router as router
            from scripts.native_short_planner_router import install_native_short_router
            from scripts.native_short_stage_contract import moment_stage_spec
            from scripts.planning_stage_contract import (
                PlanningErrorCode,
                PlanningStageError,
                request_stage_scope,
            )
            from scripts.planning_runtime_contract import (
                install_entrypoint_planning_contracts,
                install_runtime_planning_contracts,
                install_post_runtime_planning_contracts,
            )
            import scripts.planning_runtime_contract as prc

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
                prc.install_router = install_native_short_router

                install_entrypoint_planning_contracts()
                install_runtime_planning_contracts()
                install_post_runtime_planning_contracts()

                try:
                    native_short.json_text("fake-key", "unowned prompt", model="gemini-3.7-flash")
                except PlanningStageError as exc:
                    assert exc.code == PlanningErrorCode.INTERNAL_CONTRACT_ERROR, exc
                else:
                    raise AssertionError("unowned native Short provider call unexpectedly passed")

                with patch.object(router, "gemini_json_text", return_value=json.dumps(payload, ensure_ascii=False)) as gemini_mock:
                    with request_stage_scope(moment_stage_spec("short_draft", topic)):
                        result = native_short.json_text("fake-key", "test prompt", model="gemini-3.7-flash")

                assert result == payload, result
                assert gemini_mock.call_count == 1, gemini_mock.call_count
                assert router.CACHE_PATH.is_file(), "explicit Short stage did not persist durable checkpoint"
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

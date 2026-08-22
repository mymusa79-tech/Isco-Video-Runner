from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import isco_video_agent.orchestrator as orchestrator
from scripts import native_short_planner_router as router


class NativeShortPlannerRouterTests(unittest.TestCase):
    def test_router_reuses_task_provider_json_and_accepts_moment_only(self):
        original = orchestrator.build_plan
        fake_json = object()
        try:
            with patch.object(router, "install_task_router") as install_task, patch.object(
                router.resilient, "json_text", fake_json
            ), patch.object(router.native_short, "build_plan", return_value=SimpleNamespace(format="moment")) as build:
                router.install_native_short_router()
                install_task.assert_called_once_with()
                self.assertIs(router.native_short.json_text, fake_json)
                self.assertTrue(getattr(orchestrator.build_plan, "_is_resilient_router", False))
                result = orchestrator.build_plan("k", "موضوع", "moment", "model", research_context={"x": 1})
                self.assertEqual(result.format, "moment")
                build.assert_called_once()
                with self.assertRaisesRegex(router.NativeShortPlannerError, "requires_moment"):
                    orchestrator.build_plan("k", "موضوع", "film", "model")
        finally:
            orchestrator.build_plan = original

    def test_router_blocks_provider_result_that_escapes_moment(self):
        original = orchestrator.build_plan
        try:
            with patch.object(router, "install_task_router"), patch.object(
                router.resilient, "json_text", object()
            ), patch.object(router.native_short, "build_plan", return_value=SimpleNamespace(format="film")):
                router.install_native_short_router()
                with self.assertRaisesRegex(router.NativeShortPlannerError, "non_moment"):
                    orchestrator.build_plan("k", "موضوع", "moment", "model")
        finally:
            orchestrator.build_plan = original


if __name__ == "__main__":
    unittest.main()

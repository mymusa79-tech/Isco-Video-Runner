from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import scripts.task_level_planner_router as planner_router
from scripts.gemini_planning_output_guard import (
    _guarded_gemini_json_text,
    install_gemini_planning_output_guard,
)


class GeminiPlanningOutputGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original = planner_router.gemini_json_text

    def tearDown(self) -> None:
        planner_router.gemini_json_text = self.original

    def test_completed_interaction_uses_native_json_response_format(self) -> None:
        seen: dict = {}

        class FakeInteractions:
            def create(self, **kwargs):
                seen.update(kwargs)
                return SimpleNamespace(status="completed", output_text='{"ok": true}')

        client = SimpleNamespace(interactions=FakeInteractions())
        with patch("scripts.gemini_planning_output_guard.gemini_provider._client", return_value=client):
            with patch(
                "scripts.gemini_planning_output_guard.gemini_provider._content_model",
                return_value="gemini-3.5-flash-lite",
            ):
                with patch(
                    "scripts.gemini_planning_output_guard.gemini_provider.with_channel_persona",
                    side_effect=lambda prompt: prompt,
                ):
                    result = _guarded_gemini_json_text("key", "prompt", model="model")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(seen["response_format"]["type"], "text")
        self.assertEqual(seen["response_format"]["mime_type"], "application/json")
        self.assertEqual(seen["response_format"]["schema"]["type"], "object")

    def test_incomplete_interaction_is_explicit_truncation_failure(self) -> None:
        class FakeInteractions:
            def create(self, **kwargs):
                del kwargs
                return SimpleNamespace(status="incomplete", output_text='{"half":')

        client = SimpleNamespace(interactions=FakeInteractions())
        with patch("scripts.gemini_planning_output_guard.gemini_provider._client", return_value=client):
            with patch(
                "scripts.gemini_planning_output_guard.gemini_provider._content_model",
                return_value="gemini-3.5-flash-lite",
            ):
                with patch(
                    "scripts.gemini_planning_output_guard.gemini_provider.with_channel_persona",
                    side_effect=lambda prompt: prompt,
                ):
                    with self.assertRaisesRegex(RuntimeError, "INTERACTION_OUTPUT_TRUNCATED"):
                        _guarded_gemini_json_text("key", "prompt", model="model")

    def test_empty_completed_output_is_explicit_failure(self) -> None:
        class FakeInteractions:
            def create(self, **kwargs):
                del kwargs
                return SimpleNamespace(status="completed", output_text="")

        client = SimpleNamespace(interactions=FakeInteractions())
        with patch("scripts.gemini_planning_output_guard.gemini_provider._client", return_value=client):
            with patch(
                "scripts.gemini_planning_output_guard.gemini_provider._content_model",
                return_value="gemini-3.5-flash-lite",
            ):
                with patch(
                    "scripts.gemini_planning_output_guard.gemini_provider.with_channel_persona",
                    side_effect=lambda prompt: prompt,
                ):
                    with self.assertRaisesRegex(RuntimeError, "GEMINI_EMPTY_OUTPUT"):
                        _guarded_gemini_json_text("key", "prompt", model="model")

    def test_install_patches_router_global_used_by_provider_lambda(self) -> None:
        install_gemini_planning_output_guard()
        self.assertIs(planner_router.gemini_json_text, _guarded_gemini_json_text)


if __name__ == "__main__":
    unittest.main()

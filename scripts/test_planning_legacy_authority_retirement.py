from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import task_level_planner_router as router  # noqa: E402

import isco_video_agent.resilient_planner as staged  # noqa: E402


class LegacyPlanningAuthorityRetirementTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_json_text = staged.json_text
        self._old_schema_resolver = router._structured_schema_for_prompt
        staged.json_text = lambda *_args, **_kwargs: {}

    def tearDown(self) -> None:
        staged.json_text = self._old_json_text
        router._structured_schema_for_prompt = self._old_schema_resolver

    def test_prompt_inferred_schema_is_ignored_by_live_provider_helpers(self) -> None:
        prompt = 'Required number of sections: exactly 8\n"section_briefs"'
        self.assertIsNotNone(router._structured_schema_for_prompt(prompt))
        self.assertIsNone(
            router._legacy_schema_hint(prompt),
            "historical prompt parsing must not select the live provider schema",
        )

    def test_only_explicit_stage_adapter_can_supply_schema_hint(self) -> None:
        schema = {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        }

        def _explicit_schema_adapter(_prompt: str):
            return "explicit_test_contract", schema

        router._structured_schema_for_prompt = _explicit_schema_adapter
        self.assertEqual(
            router._legacy_schema_hint("arbitrary prompt wording"),
            ("explicit_test_contract", schema),
        )

    def test_install_router_never_reads_or_writes_legacy_checkpoint(self) -> None:
        def fake_gemini_json_text(api_key, prompt, model):
            del api_key, prompt, model
            return {"ok": True}

        with patch.object(
            router,
            "_load_checkpoint",
            side_effect=AssertionError("legacy checkpoint read regained authority"),
        ), patch.object(
            router,
            "_save_checkpoint",
            side_effect=AssertionError("legacy checkpoint write regained authority"),
        ), patch.object(
            router,
            "gemini_json_text",
            side_effect=fake_gemini_json_text,
        ):
            router.install_router()
            result = staged.json_text(
                "request-scoped-key",
                "نداء اليقظة: اختبار سلطة التخطيط",
                model="gemini-2.5-flash",
            )

        self.assertEqual(result, {"ok": True})


if __name__ == "__main__":
    unittest.main()

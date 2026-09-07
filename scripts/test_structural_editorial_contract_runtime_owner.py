from __future__ import annotations

import inspect
import unittest

from scripts import structural_editorial_contract


class StructuralEditorialRuntimeOwnerTests(unittest.TestCase):
    def test_module_contains_no_direct_provider_or_retry_implementation(self) -> None:
        source = inspect.getsource(structural_editorial_contract).casefold()
        for forbidden in (
            "openrouter",
            "groq",
            "gemini",
            "json_text(",
            "task_router(",
            "time.sleep",
            "retry_after",
        ):
            self.assertNotIn(forbidden, source)

    def test_issue_owner_reuses_engine_detector(self) -> None:
        source = inspect.getsource(structural_editorial_contract)
        self.assertIn("from isco_video_agent.editorial_room import structural_ai_flags", source)
        self.assertIn("staged._section_length_issue_notes", source)
        self.assertIn("staged.build_plan", source)


if __name__ == "__main__":
    unittest.main()

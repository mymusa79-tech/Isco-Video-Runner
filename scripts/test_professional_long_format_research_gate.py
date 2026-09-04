from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.control_approved_brief import materialize_approved_brief


class ProfessionalLongFormatResearchGateTests(unittest.TestCase):
    def test_auto_long_cannot_bypass_research_pack_gate(self) -> None:
        request = {
            "kind": "long",
            "approved_topic": "لماذا نفقد الدافع؟",
            "format": "auto",
            "format_policy": {
                "version": "professional_long_format_router_v1",
                "requested": "auto",
                "resolution_stage": "v4_before_approved_brief_binding",
            },
            "research_pack": [],
        }
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(RuntimeError, "completed approved research pack"):
                materialize_approved_brief(request, Path(td) / "brief.json")


if __name__ == "__main__":
    unittest.main()

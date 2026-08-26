from __future__ import annotations

import unittest
from pathlib import Path

from scripts.planning_envelope_preflight import certify_planning_envelope


ROOT = Path(__file__).resolve().parents[1]


class PlanningEnvelopePreflightTests(unittest.TestCase):
    def test_exact_pinned_engine_envelope_is_certified_without_inference(self) -> None:
        result = certify_planning_envelope()
        self.assertEqual(result.status, "pass")
        self.assertGreater(result.prompt_utf8_bytes, 0)
        self.assertGreater(result.remaining_headroom_utf8_bytes, 0)
        self.assertGreaterEqual(result.approved_sources, 2)
        self.assertGreater(result.approved_boundaries, 0)

    def test_certification_runs_before_production(self) -> None:
        workflow = (ROOT / ".github/workflows/produce-resilient-v4.yml").read_text(
            encoding="utf-8"
        )
        certification = workflow.index("Certify provider-portable planning envelope")
        production = workflow.index("Produce with task-level brain and voice meshes")
        self.assertLess(certification, production)
        self.assertIn("scripts/planning_envelope_preflight.py", workflow)


if __name__ == "__main__":
    unittest.main()
